#!/usr/bin/env bash
# Creates eabu-recruit: one row per business emailed by scripts/recruit.py send, keyed by
# emailHash (sha256 of the lower-case email; the email itself is never stored), plus
# daily#YYYY-MM-DD counter rows. No TTL: a business that was emailed or unsubscribed must never
# be emailed again. Safe to re-run.
set -euo pipefail
TABLE="${1:-eabu-recruit}"
REGION="${AWS_REGION:-us-east-1}"
if ! aws dynamodb describe-table --table-name "$TABLE" --region "$REGION" >/dev/null 2>&1; then
  aws dynamodb create-table --table-name "$TABLE" --region "$REGION" --billing-mode PAY_PER_REQUEST \
    --attribute-definitions AttributeName=emailHash,AttributeType=S \
    --key-schema AttributeName=emailHash,KeyType=HASH >/dev/null
  aws dynamodb wait table-exists --table-name "$TABLE" --region "$REGION"
  echo "Created $TABLE"
else
  echo "$TABLE already exists"
fi
