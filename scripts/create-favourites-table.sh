#!/usr/bin/env bash
# Create the eabu-favourites DynamoDB table (not part of the SAM stack).
# Key: userId (who saved) + favKey ("seller#<id>" or "buyer#<id>").
#
#   ./scripts/create-favourites-table.sh            # report only, changes nothing
#   ./scripts/create-favourites-table.sh --apply    # create it if missing
#
# Run this BEFORE `sam deploy`. An existing table is left alone.
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
TABLE="${TABLE:-eabu-favourites}"
[[ $# -eq 0 || "${1:-}" == "--apply" ]] || { echo "usage: $0 [--apply]" >&2; exit 2; }

if aws dynamodb describe-table --region "$REGION" --table-name "$TABLE" >/dev/null 2>&1; then
  echo "ok       $TABLE"; exit 0
fi
if [[ "${1:-}" != "--apply" ]]; then echo "missing  $TABLE"; exit 0; fi

aws dynamodb create-table --region "$REGION" --table-name "$TABLE" --billing-mode PAY_PER_REQUEST \
  --attribute-definitions AttributeName=userId,AttributeType=S AttributeName=favKey,AttributeType=S \
  --key-schema AttributeName=userId,KeyType=HASH AttributeName=favKey,KeyType=RANGE >/dev/null
aws dynamodb wait table-exists --region "$REGION" --table-name "$TABLE"
echo "created  $TABLE"
