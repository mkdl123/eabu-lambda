#!/usr/bin/env bash
# Create eabu-blocked-emails: {emailHash, blockedAt, expiresAt} for emails of recently deleted
# accounts (SHA-256 of the lowercased email, never the address). DynamoDB TTL on expiresAt
# removes each row when its 30-day block ends. Not part of the SAM stack, like the other tables.
#
#   ./scripts/create-blocked-emails-table.sh            # report only, changes nothing
#   ./scripts/create-blocked-emails-table.sh --apply    # create it (and its TTL) if missing
#
# Run this BEFORE `sam deploy`: account deletion writes to it and the sign-up trigger reads it.
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
NAME="eabu-blocked-emails"
APPLY=false
[[ "${1:-}" == "--apply" ]] && APPLY=true
[[ $# -eq 0 || "${1:-}" == "--apply" ]] || { echo "usage: $0 [--apply]" >&2; exit 2; }

if aws dynamodb describe-table --region "$REGION" --table-name "$NAME" >/dev/null 2>&1; then
  echo "ok       $NAME"; exit 0
fi
if ! $APPLY; then echo "missing  $NAME"; exit 0; fi
aws dynamodb create-table --region "$REGION" --table-name "$NAME" --billing-mode PAY_PER_REQUEST \
  --attribute-definitions AttributeName=emailHash,AttributeType=S \
  --key-schema AttributeName=emailHash,KeyType=HASH >/dev/null
aws dynamodb wait table-exists --region "$REGION" --table-name "$NAME"
aws dynamodb update-time-to-live --region "$REGION" --table-name "$NAME" \
  --time-to-live-specification Enabled=true,AttributeName=expiresAt >/dev/null
echo "created  $NAME (TTL on expiresAt)"
