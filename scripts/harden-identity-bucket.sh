#!/usr/bin/env bash
# Harden the S3 bucket that stores users' optional profile pictures (it held identity
# selfies and driver's licence images until those uploads were removed).
#
# Designed to run in AWS CloudShell (AWS CLI + python3 preinstalled).
#
#   ./scripts/harden-identity-bucket.sh                      # report only, changes nothing
#   ./scripts/harden-identity-bucket.sh --apply              # apply the bucket hardening
#   ./scripts/harden-identity-bucket.sh --apply --log-retention-days 30 --stack eabulambda
#       also sets CloudWatch retention on the profile-picture function's log group.
#       WARNING: shortening retention permanently deletes older log events. That is the
#       intent here (old logs contain base64 license images), but it cannot be undone.
#
# What --apply does (each step is idempotent):
#   1. Block Public Access - all four settings on.
#   2. Object Ownership = BucketOwnerEnforced (disables ACLs, so no object can be made public by ACL).
#   3. Default encryption SSE-S3 (AES256) with S3 Bucket Key.
#   4. Bucket policy statements (merged into any existing policy, matched by Sid):
#        - EabuDenyInsecureTransport: deny any request not using TLS.
#        - EabuDenyOldPresignedUrls:  deny GetObject with a signature older than 10 minutes,
#          so a leaked presigned link stops working even if code issues a long expiry.
#   5. Lifecycle rule to abort incomplete multipart uploads after 1 day
#      (only if the bucket has no lifecycle configuration yet - never overwrites one).
# Versioning is reported but not changed (see the note printed at the end).
set -euo pipefail

BUCKET="eabu-identity-pictures"
APPLY=false
LOG_DAYS=""
STACK=""
LOG_FUNCTION_LOGICAL_ID="EabuProfilePictureFunction"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=true ;;
    --bucket) BUCKET="$2"; shift ;;
    --log-retention-days) LOG_DAYS="$2"; shift ;;
    --stack) STACK="$2"; shift ;;
    -h|--help) sed -n '2,27p' "$0"; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

if [[ -n "$LOG_DAYS" && -z "$STACK" ]]; then
  echo "--log-retention-days needs --stack <stack-name> to find the function's log group" >&2; exit 2
fi

echo "== Account: $(aws sts get-caller-identity --query Account --output text)  Bucket: s3://$BUCKET"
aws s3api head-bucket --bucket "$BUCKET"

echo; echo "== Current state"
echo "-- Public access block:"; aws s3api get-public-access-block --bucket "$BUCKET" --query PublicAccessBlockConfiguration 2>/dev/null || echo "   (none configured)"
echo "-- Object ownership:";    aws s3api get-bucket-ownership-controls --bucket "$BUCKET" --query 'OwnershipControls.Rules[0].ObjectOwnership' --output text 2>/dev/null || echo "   (not set - ACLs enabled)"
echo "-- Encryption:";          aws s3api get-bucket-encryption --bucket "$BUCKET" --query 'ServerSideEncryptionConfiguration.Rules[0]' 2>/dev/null || echo "   (none)"
echo "-- Versioning:";          VERSIONING=$(aws s3api get-bucket-versioning --bucket "$BUCKET" --query Status --output text); echo "   $VERSIONING"
echo "-- Policy status:";       aws s3api get-bucket-policy-status --bucket "$BUCKET" --query PolicyStatus 2>/dev/null || echo "   (no bucket policy)"
EXISTING_POLICY=$(aws s3api get-bucket-policy --bucket "$BUCKET" --query Policy --output text 2>/dev/null || echo "")
echo "-- Bucket policy:"; if [[ -n "$EXISTING_POLICY" ]]; then echo "$EXISTING_POLICY" | python3 -m json.tool; else echo "   (none)"; fi
HAS_LIFECYCLE=true
aws s3api get-bucket-lifecycle-configuration --bucket "$BUCKET" >/dev/null 2>&1 || HAS_LIFECYCLE=false
echo "-- Lifecycle configuration present: $HAS_LIFECYCLE"

if [[ "$APPLY" != true ]]; then
  echo; echo "Report only. Re-run with --apply to make changes."; exit 0
fi

echo; echo "== Applying"
aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
echo "ok  public access blocked"

aws s3api put-bucket-ownership-controls --bucket "$BUCKET" \
  --ownership-controls 'Rules=[{ObjectOwnership=BucketOwnerEnforced}]'
echo "ok  ACLs disabled (BucketOwnerEnforced)"

# Skip when SSE-S3 + Bucket Key is already set: put-bucket-encryption replaces the
# whole config and would drop settings such as BlockedEncryptionTypes (SSE-C block).
CURRENT_ENC=$(aws s3api get-bucket-encryption --bucket "$BUCKET" \
  --query 'ServerSideEncryptionConfiguration.Rules[0].[ApplyServerSideEncryptionByDefault.SSEAlgorithm,BucketKeyEnabled]' \
  --output text 2>/dev/null || echo "")
if [[ "$CURRENT_ENC" == $'AES256\tTrue' ]]; then
  echo "skip default encryption: SSE-S3 with Bucket Key already set (keeping existing config)"
else
  aws s3api put-bucket-encryption --bucket "$BUCKET" --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":true}]}'
  echo "ok  default encryption SSE-S3"
fi

NEW_POLICY=$(BUCKET="$BUCKET" EXISTING_POLICY="$EXISTING_POLICY" python3 - <<'PY'
import json, os
bucket = os.environ["BUCKET"]
existing = os.environ.get("EXISTING_POLICY") or ""
policy = json.loads(existing) if existing else {"Version": "2012-10-17", "Statement": []}
ours = [
    {
        "Sid": "EabuDenyInsecureTransport",
        "Effect": "Deny",
        "Principal": "*",
        "Action": "s3:*",
        "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"],
        "Condition": {"Bool": {"aws:SecureTransport": "false"}},
    },
    {
        "Sid": "EabuDenyOldPresignedUrls",
        "Effect": "Deny",
        "Principal": "*",
        "Action": "s3:GetObject",
        "Resource": f"arn:aws:s3:::{bucket}/*",
        "Condition": {"NumericGreaterThan": {"s3:signatureAge": "600000"}},
    },
]
sids = {s["Sid"] for s in ours}
stmts = policy.get("Statement", [])
if isinstance(stmts, dict):
    stmts = [stmts]
policy["Statement"] = [s for s in stmts if s.get("Sid") not in sids] + ours
print(json.dumps(policy))
PY
)
aws s3api put-bucket-policy --bucket "$BUCKET" --policy "$NEW_POLICY"
echo "ok  bucket policy: TLS-only, presigned links older than 10 minutes refused (existing statements kept)"

if [[ "$HAS_LIFECYCLE" == false ]]; then
  aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" --lifecycle-configuration \
    '{"Rules":[{"ID":"EabuAbortIncompleteUploads","Status":"Enabled","Filter":{},"AbortIncompleteMultipartUpload":{"DaysAfterInitiation":1}}]}'
  echo "ok  lifecycle: abort incomplete uploads after 1 day"
else
  echo "skip lifecycle: bucket already has a lifecycle configuration (not overwriting)"
fi

if [[ -n "$LOG_DAYS" ]]; then
  FN=$(aws cloudformation describe-stack-resource --stack-name "$STACK" \
        --logical-resource-id "$LOG_FUNCTION_LOGICAL_ID" --query StackResourceDetail.PhysicalResourceId --output text)
  aws logs put-retention-policy --log-group-name "/aws/lambda/$FN" --retention-in-days "$LOG_DAYS"
  echo "ok  /aws/lambda/$FN retention set to $LOG_DAYS days (older events are deleted by CloudWatch)"
fi

echo
if [[ "$VERSIONING" == "Enabled" || "$VERSIONING" == "Suspended" ]]; then
  echo "NOTE: versioning is $VERSIONING. Overwritten or deleted ID images survive as noncurrent"
  echo "      versions. If a user asks for deletion, their noncurrent versions must be removed too."
fi
echo "Done."
