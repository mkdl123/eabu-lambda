#!/usr/bin/env bash
# Give every CloudWatch log group without a retention period one (default 30 days).
# Lambda creates a function's log group on its first run with "never expire", so run this
# after deploying new functions. Safe to re-run: groups that already have a retention are left alone.
#
#   scripts/set-log-retention.sh          # 30 days
#   scripts/set-log-retention.sh 14       # 14 days
set -euo pipefail
DAYS="${1:-30}"
REGION="${AWS_REGION:-us-east-1}"

groups=$(aws logs describe-log-groups --region "$REGION" \
  --query 'logGroups[?!retentionInDays].logGroupName' --output text)

count=0
for g in $groups; do
  aws logs put-retention-policy --region "$REGION" --log-group-name "$g" --retention-in-days "$DAYS"
  echo "set ${DAYS}d: $g"
  count=$((count + 1))
done
echo "Done: $count log group(s) updated."
