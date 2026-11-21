#!/usr/bin/env bash
# Create eabu-discovered: the weekly "Discovered near you" food finds, one partition per area
# ({areaZip, ID}; ID 0 is the area itself). DynamoDB TTL on expiresAt removes finds after
# 8 days. Not part of the SAM stack, like the other tables.
#
#   ./scripts/create-discovered-table.sh            # report only, changes nothing
#   ./scripts/create-discovered-table.sh --apply    # create it if missing
#
# Run this BEFORE `sam deploy`. Nothing here is user data, so no point-in-time recovery.
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
NAME="eabu-discovered"
APPLY=false
[[ "${1:-}" == "--apply" ]] && APPLY=true
[[ $# -eq 0 || "${1:-}" == "--apply" ]] || { echo "usage: $0 [--apply]" >&2; exit 2; }

if aws dynamodb describe-table --region "$REGION" --table-name "$NAME" >/dev/null 2>&1; then
  echo "ok       $NAME"; exit 0
fi
if ! $APPLY; then echo "missing  $NAME"; exit 0; fi
aws dynamodb create-table --region "$REGION" --table-name "$NAME" --billing-mode PAY_PER_REQUEST \
  --attribute-definitions AttributeName=areaZip,AttributeType=S AttributeName=ID,AttributeType=N \
  --key-schema AttributeName=areaZip,KeyType=HASH AttributeName=ID,KeyType=RANGE >/dev/null
aws dynamodb wait table-exists --region "$REGION" --table-name "$NAME"
aws dynamodb update-time-to-live --region "$REGION" --table-name "$NAME" \
  --time-to-live-specification Enabled=true,AttributeName=expiresAt >/dev/null
echo "created  $NAME (TTL on expiresAt)"
