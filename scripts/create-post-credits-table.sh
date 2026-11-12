#!/usr/bin/env bash
# Create eabu-post-credits: each seller's extra-post balance ({userId, balance}) and the
# Apple transactions already credited ({userId: "tx#<transactionId>"}). Not part of the
# SAM stack, like the other tables.
#
#   ./scripts/create-post-credits-table.sh            # report only, changes nothing
#   ./scripts/create-post-credits-table.sh --apply    # create it if missing
#
# Run this BEFORE `sam deploy`: past the free monthly limit, every seller post reads it.
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
NAME="eabu-post-credits"
APPLY=false
[[ "${1:-}" == "--apply" ]] && APPLY=true
[[ $# -eq 0 || "${1:-}" == "--apply" ]] || { echo "usage: $0 [--apply]" >&2; exit 2; }

if aws dynamodb describe-table --region "$REGION" --table-name "$NAME" >/dev/null 2>&1; then
  echo "ok       $NAME"; exit 0
fi
if ! $APPLY; then echo "missing  $NAME"; exit 0; fi
aws dynamodb create-table --region "$REGION" --table-name "$NAME" --billing-mode PAY_PER_REQUEST \
  --attribute-definitions AttributeName=userId,AttributeType=S \
  --key-schema AttributeName=userId,KeyType=HASH >/dev/null
aws dynamodb wait table-exists --region "$REGION" --table-name "$NAME"
# Paid balances: keep point-in-time recovery on so a mistake can be undone.
aws dynamodb update-continuous-backups --region "$REGION" --table-name "$NAME" \
  --point-in-time-recovery-specification PointInTimeRecoveryEnabled=true >/dev/null
echo "created  $NAME"
