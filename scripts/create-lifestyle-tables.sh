#!/usr/bin/env bash
# Create the DynamoDB tables for the Lifestyle category. The tables are not part of the
# SAM stack; their keys and indexes match the eabu-live-* tables.
#
#   ./scripts/create-lifestyle-tables.sh            # report only, changes nothing
#   ./scripts/create-lifestyle-tables.sh --apply    # create the tables that are missing
#
# Run this BEFORE `sam deploy`: eabu-get-user-ratings reads eabu-lifestyle-provider for
# every provider, so ratings fail if the code goes out first. Existing tables are left alone.
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
APPLY=false
[[ "${1:-}" == "--apply" ]] && APPLY=true
[[ $# -eq 0 || "${1:-}" == "--apply" ]] || { echo "usage: $0 [--apply]" >&2; exit 2; }

exists() { aws dynamodb describe-table --region "$REGION" --table-name "$1" >/dev/null 2>&1; }

create() { # name, then create-table arguments
  local name="$1"; shift
  if exists "$name"; then echo "ok       $name"; return; fi
  if ! $APPLY; then echo "missing  $name"; return; fi
  aws dynamodb create-table --region "$REGION" --table-name "$name" \
    --billing-mode PAY_PER_REQUEST "$@" >/dev/null
  aws dynamodb wait table-exists --region "$REGION" --table-name "$name"
  echo "created  $name"
}

ZIP_ID_ATTRS=(AttributeName=zipcode,AttributeType=S AttributeName=ID,AttributeType=N AttributeName=userId,AttributeType=S)
ZIP_ID_KEYS=(AttributeName=zipcode,KeyType=HASH AttributeName=ID,KeyType=RANGE)
USER_INDEX='{"IndexName":"userId-index","KeySchema":[{"AttributeName":"userId","KeyType":"HASH"}],"Projection":{"ProjectionType":"ALL"}}'
USER_ITEM_INDEX='{"IndexName":"userId-itemId-index","KeySchema":[{"AttributeName":"userId","KeyType":"HASH"},{"AttributeName":"itemId","KeyType":"RANGE"}],"Projection":{"ProjectionType":"ALL"}}'

for table in eabu-lifestyle-consumer eabu-lifestyle-provider; do
  create "$table" --attribute-definitions "${ZIP_ID_ATTRS[@]}" --key-schema "${ZIP_ID_KEYS[@]}" \
    --global-secondary-indexes "[$USER_INDEX]"
done

create eabu-lifestyle-offer \
  --attribute-definitions "${ZIP_ID_ATTRS[@]}" AttributeName=itemId,AttributeType=N \
  --key-schema "${ZIP_ID_KEYS[@]}" \
  --global-secondary-indexes "[$USER_INDEX,$USER_ITEM_INDEX]"

for table in eabu-lifestyle-interested eabu-lifestyle-rated; do
  create "$table" \
    --attribute-definitions AttributeName=ID,AttributeType=N AttributeName=cUserId,AttributeType=S \
    --key-schema AttributeName=ID,KeyType=HASH AttributeName=cUserId,KeyType=RANGE
done

$APPLY || echo "Report only. Re-run with --apply to create missing tables."
