import json
import os
from my_utils import helpers

import boto3
import logging
import base64
from datetime import datetime
from typing import Dict, Any
import io
import re
import warnings
from PIL import Image, ExifTags
from botocore.exceptions import BotoCoreError, ClientError

# Set up logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Initialize S3 client
s3_client = boto3.client('s3')
BUCKET_NAME = os.environ.get('IDENTITY_BUCKET_NAME', 'eabu-identity-pictures')
MAX_FILE_SIZE = 10 * 1024 * 1024
ALLOWED_IMAGE_FORMATS = ["JPEG", "PNG", "WEBP"]
# userId becomes an S3 key prefix, so restrict it to a safe character set (no '/', no '..').
USER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_@.+:-]{1,128}$")

# Guard against decompression bombs: refuse anything over ~50 megapixels.
Image.MAX_IMAGE_PIXELS = 50_000_000
warnings.simplefilter("error", Image.DecompressionBombWarning)

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
        
        # Verify it's a valid image in an allowed format
        img = Image.open(io.BytesIO(image_data), formats=ALLOWED_IMAGE_FORMATS)
        img.verify()

        return image_data
    except Exception as e:
        logger.warning("Rejected %s image: %s", image_type, e)
        raise ValueError(f"Invalid {image_type} image")

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
        exif = img.getexif()
        if exif:
            orientation_value = exif.get(orientation)
            if orientation_value == 3:
                image = img.rotate(180, expand=True)
            elif orientation_value == 6:
                image = img.rotate(270, expand=True)
            elif orientation_value == 8:
                image = img.rotate(90, expand=True)
        # Save optimized image (re-encoding also strips EXIF metadata such as GPS location)
        if image.mode != 'RGB':
            image = image.convert('RGB')
        buffer = io.BytesIO()
        image.save(buffer, format='JPEG', quality=90, optimize=True)
        return buffer.getvalue()
    except Exception as e:
        # Never upload the raw, unprocessed bytes if re-encoding failed.
        logger.error("Image optimization failed: %s", e)
        raise ValueError("Image could not be processed")

def upload_to_s3(image_data: bytes, user_id: str, image_type: str, timestamp: str) -> str:
    """Upload image to S3 and return its key"""
    try:
        #s3_key = f"users/{user_id}/{timestamp}/{image_type}.jpg"
        s3_key = f"{user_id}/{image_type}.jpg"
        s3_client.put_object(
            Bucket=BUCKET_NAME,
            Key=s3_key,
            Body=image_data,
            ContentType='image/jpeg'
        )
        
        return s3_key
    except Exception as e:
        logger.error("Failed to upload %s: %s", image_type, e)
        raise Exception(f"Failed to upload {image_type}")

@helpers.cors
def lambda_handler(event, context):
    try:
        # Parse JSON body
        if not event.get('body'):
            return {
                'statusCode': 400,
                'body': json.dumps({'error': 'Request body is required'})
            }
            
        try:
            body = helpers._parse_body(event)
        except ValueError:
            return {
                'statusCode': 400,
                'body': json.dumps({'error': 'Invalid JSON format'})
            }
        if not isinstance(body, dict):
            return {
                'statusCode': 400,
                'body': json.dumps({'error': 'Invalid JSON format'})
            }

        # Validate required fields
        required_fields = ['user_id', 'selfie', 'license_front', 'license_back', 'address', 'full_name', 'email', 'phone']
        missing_fields = [field for field in required_fields if field not in body]

        if missing_fields:
            return {
                'statusCode': 400,
                'body': json.dumps({
                    'error': f'Missing required fields: {", ".join(missing_fields)}'
                })
            }

        user_id = body['user_id']
        if not isinstance(user_id, str) or not USER_ID_PATTERN.match(user_id) or ".." in user_id:
            return helpers._response(400, {"msg": "Invalid user_id"})

        # Only the signed-in user may submit their own identity documents.
        denied = helpers.authorize_user(event, user_id)
        if denied:
            return denied

        # Do not let a submission overwrite an identity that has already been verified.
        try:
            existing = table.query(
                KeyConditionExpression="userId = :userId",
                ExpressionAttributeValues={':userId': user_id},
                Limit=1,
            ).get('Items', [])
        except (BotoCoreError, ClientError):
            logger.exception("Failed to read existing identity record")
            return helpers._response(500, {"msg": "Failed to enter User Identity record"})
        if existing and existing[0].get('verified') is True:
            return helpers._response(409, {"msg": "Identity already verified; contact support to change it"})

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        uploaded_files = {}

        # Validate and re-encode every image before uploading any of them,
        # so a bad image cannot leave a partially-overwritten set in S3.
        processed = {}
        for image_type in ('selfie', 'license_front', 'license_back'):
            base64_data = body[image_type]
            if not isinstance(base64_data, str):
                return helpers._response(400, {"msg": f"Error processing {image_type}"})
            try:
                image_data = validate_image_base64(base64_data, image_type)
                processed[image_type] = optimize_image(image_data)
            except Exception:
                return {
                    'statusCode': 400,
                    'body': json.dumps({'error': f'Error processing {image_type}'})
                }

        for image_type, optimized_data in processed.items():
            try:
                uploaded_files[image_type] = upload_to_s3(optimized_data, user_id, image_type, timestamp)
            except Exception:
                return helpers._response(500, {"msg": f"Failed to upload {image_type}"})

        try:
            # go for a record entry in eabu-identity table:
            item = {
                "userId": user_id,
                "submitted": True,
                "verified": False,
                "fullname": str(body["full_name"])[:200],
                "email": str(body["email"])[:254],
                "phone": str(body["phone"])[:32],
                "address": str(body["address"])[:500],
            }
            table.put_item(Item=item)
        except (BotoCoreError, ClientError) as e:
            logger.exception("Failed to write item to DynamoDB")
            return helpers._response(500, {
                "msg": "Failed to enter User Identity record"})

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
        logger.exception("Lambda execution failed")
        return {
            'statusCode': 500,
            'body': json.dumps({
                'error': 'Internal server error'
            }),
            'headers': {
                'Content-Type': 'application/json'
            }
        }

