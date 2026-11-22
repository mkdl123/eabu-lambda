#!/usr/bin/env bash
# Creates the per-user hourly read counter table (anti-scraping, helpers.rate_limited) with TTL
# on expiresAt, so old hours delete themselves. Safe to re-run.
set -euo pipefail
TABLE="${1:-eabu-rate-limit}"
REGION="${AWS_REGION:-us-east-1}"
if ! aws dynamodb describe-table --table-name "$TABLE" --region "$REGION" >/dev/null 2>&1; then
  aws dynamodb create-table --table-name "$TABLE" --region "$REGION" --billing-mode PAY_PER_REQUEST \
    --attribute-definitions AttributeName=key,AttributeType=S --key-schema AttributeName=key,KeyType=HASH >/dev/null
  aws dynamodb wait table-exists --table-name "$TABLE" --region "$REGION"
  echo "Created $TABLE"
fi
aws dynamodb update-time-to-live --table-name "$TABLE" --region "$REGION" \
  --time-to-live-specification Enabled=true,AttributeName=expiresAt >/dev/null 2>&1 || true
aws dynamodb describe-time-to-live --table-name "$TABLE" --region "$REGION" --query "TimeToLiveDescription.TimeToLiveStatus" --output text
