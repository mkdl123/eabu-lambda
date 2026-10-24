import json
import os
from my_utils import helpers

import boto3
import logging
import base64
from datetime import datetime
from typing import Dict, Any
import io
from PIL import Image, ExifTags
from botocore.exceptions import BotoCoreError, ClientError

# Set up logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Initialize S3 client
s3_client = boto3.client('s3')
BUCKET_NAME = 'eabu-identity-pictures'
MAX_FILE_SIZE = 10 * 1024 * 1024

dynamodb = boto3.resource('dynamodb')
table1 = dynamodb.Table('eabu-identity') # Replace 'YourTableName'
# Resolve environment configuration
table_name = os.environ.get('EABU_IDENTITY_TABLE_NAME', 'eabu-identity')
region = os.environ.get('REGION_NAME', 'us-east-1')
dynamodb = boto3.resource("dynamodb", region_name=region)
table = dynamodb.Table(table_name)


def validate_image_base64(base64_data: str, image_type: str) -> bytes:
    """Validate and convert base64 image data"""
    try:
        # Remove data URL prefix if present
        if ',' in base64_data:
            base64_data = base64_data.split(',')[1]
            
        # Decode base64
        image_data = base64.b64decode(base64_data)
        
        # Check file size
        if len(image_data) > MAX_FILE_SIZE:
            raise ValueError(f"{image_type} image exceeds maximum size of 10MB")
        
        # Verify it's a valid image
        img = Image.open(io.BytesIO(image_data))
        img.verify()
        
        return image_data
    except Exception as e:
        raise ValueError(f"Invalid {image_type} image: {str(e)}")

def optimize_image(image_data: bytes, max_size: tuple = (1920, 1080)) -> bytes:
    """Optimize image before upload"""
    try:
        img = Image.open(io.BytesIO(image_data))
        
        # Convert to RGB if necessary
        if img.mode in ('RGBA', 'P'):
            img = img.convert('RGB')
        
        # Resize if larger than max_size
        if img.size[0] > max_size[0] or img.size[1] > max_size[1]:
            img.thumbnail(max_size, Image.Resampling.LANCZOS)
        #fix any rotation issue
        for orientation in ExifTags.TAGS.keys():
            if ExifTags.TAGS[orientation] == 'Orientation':
                break

        image = img;
        exif = img._getexif()
        if exif is not None:
            print("Exif found")
            orientation_value = exif.get(orientation)
            if orientation_value == 3:
                image = img.rotate(180, expand=True)
            elif orientation_value == 6:
                image = img.rotate(270, expand=True)
            elif orientation_value == 8:
                image = img.rotate(90, expand=True)
        # Save optimized image
        buffer = io.BytesIO()
        image.save(buffer, format='JPEG', quality=90, optimize=True)
        return buffer.getvalue()
    except Exception as e:
        logger.error(f"Image optimization failed: {str(e)}")
        return image_data

def upload_to_s3(image_data: bytes, user_id: str, image_type: str, timestamp: str) -> str:
    """Upload image to S3 and return the URL"""
    try:
        #s3_key = f"users/{user_id}/{timestamp}/{image_type}.jpg"
        s3_key = f"{user_id}/{image_type}.jpg"
        s3_client.put_object(
            Bucket=BUCKET_NAME,
            Key=s3_key,
            Body=image_data,
            ContentType='image/jpeg'
        )
        
        return f"s3://{BUCKET_NAME}/{s3_key}"
    except Exception as e:
        raise Exception(f"Failed to upload {image_type}: {str(e)}")

def lambda_handler(event, context):
    try:
        # Parse JSON body
        if not event.get('body'):
            print("No body found")
            return {
                'statusCode': 400,
                'body': json.dumps({'error': 'Request body is required'})
            }
            
        try:
            body = json.loads(event['body'])
            print("Body: ",body)
        except json.JSONDecodeError:
            return {
                'statusCode': 400,
                'body': json.dumps({'error': 'Invalid JSON format'})
            }
        
        # Validate required fields
        required_fields = ['user_id', 'selfie', 'license_front', 'license_back', 'address', 'full_name', 'email', 'phone']
        missing_fields = [field for field in required_fields if field not in body]
        
        if missing_fields:
            print("Missing fields: ",missing_fields)
            return {
                'statusCode': 400,
                'body': json.dumps({
                    'error': f'Missing required fields: {", ".join(missing_fields)}'
                })
            }
        
        user_id = body['user_id']
        print("User ID: ",user_id);
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        print("Timestamp: ",timestamp);
        uploaded_files = {}
        
        # Process each image
        images_to_process = {
            'selfie': body['selfie'],
            'license_front': body['license_front'],
            'license_back': body['license_back']
        }

        for image_type, base64_data in images_to_process.items():
            try:
                # Validate and decode base64 image
                image_data = validate_image_base64(base64_data, image_type)
                
                # Optimize image
                optimized_data = optimize_image(image_data)
                
                # Upload to S3
                s3_url = upload_to_s3(optimized_data, user_id, image_type, timestamp)
                
                uploaded_files[image_type] = s3_url
                
            except Exception as e:
                return {
                    'statusCode': 400,
                    'body': json.dumps({
                        'error': f'Error processing {image_type}',
                        'details': str(e)
                    })
                }


        try:
            # go for a record entry in eabu-identity table:
            payload = helpers._parse_body(event)
            item = {

                "userId": user_id,
                "submitted": True,
                "verified": False,
                "fullname":payload["full_name"] if isinstance(payload["full_name"], str) else str(payload["full_name"]),
                "email": payload["email"] if isinstance(payload["email"], str) else str(payload["email"]),
                "phone": payload["phone"] if isinstance(payload["phone"], str) else str(payload["phone"]),
                "address": payload["address"] if isinstance(payload["address"], str) else str(payload["address"]),
            }
            table.put_item(Item=item)
        except (BotoCoreError, ClientError) as e:
            logger.exception("Failed to write item to DynamoDB")
            return helpers._response(500, {
                "msg": "Failed to enter User Idenity record" + "Table Name: " + table_name + " Region: " + region})

        #return helpers._response(200, {"message": "Recipe posted ", "ID": item["ID"]})
        # Return success response
        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'Images uploaded successfully',
                'userId': user_id,
                'timestamp': timestamp,
                'files': uploaded_files
            }),
            'headers': {
                'Content-Type': 'application/json'
            }
        }
        
    except Exception as e:
        logger.error(f"Lambda execution failed: {str(e)}")
        return {
            'statusCode': 500,
            'body': json.dumps({
                'error': 'Internal server error',
                'details': str(e)
            }),
            'headers': {
                'Content-Type': 'application/json'
            }
        }

