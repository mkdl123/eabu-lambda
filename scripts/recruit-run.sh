#!/usr/bin/env bash
# One command for seller recruitment (wraps scripts/recruit.py).
#
#   ./scripts/recruit-run.sh <zip> <category> test    # make the list if needed, send ONE preview to admin@eabu.net
#   ./scripts/recruit-run.sh <zip> <category> send    # show who gets it, ask, then email the businesses (max 50/day)
#   ./scripts/recruit-run.sh <zip> <category> new     # make a fresh list (replaces today's list for that ZIP/category)
#   ./scripts/recruit-run.sh <file.csv> test          # same, with a list you choose (never searches)
#   ./scripts/recruit-run.sh <file.csv> send
#   ./scripts/recruit-run.sh ad.xlsx test|send        # emails in column A get docs/marketing-email-business.html
#   ./scripts/recruit-run.sh seller.xlsx test|send    # emails in column A get docs/marketing-email-sellers.html
#
#   e.g. ./scripts/recruit-run.sh 33130 catering test
#        ./scripts/recruit-run.sh recruit/33130-catering-2026-11-27.csv send
#
# Categories: cleaning catering home moving fitness beauty pets tutoring childcare all
# The list is recruit/<zip>-<category>-<date>.csv. Open it in Numbers, delete rows you
# don't want and save BEFORE "send". test/send always reuse the newest list for that
# ZIP/category, so your edits are kept; only "new" (or no list yet) searches again.
set -euo pipefail

usage() { sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
PREVIEW_TO="${PREVIEW_TO:-admin@eabu.net}"

if [[ $# -eq 2 && ( "$1" == *.csv || "$1" == *.xlsx ) ]]; then
  # a list given by the user: make its path absolute before changing folder
  FILE="$1"
  # a bare name like ad.xlsx: also look in the repo's recruit/ folder
  [[ -f "$FILE" ]] || FILE="$(cd "$(dirname "$0")/.." && pwd)/recruit/$1"
  [[ -f "$FILE" ]] || { echo "No such file: $1 (not here or in recruit/)"; exit 2; }
  LIST="$(cd "$(dirname "$FILE")" && pwd)/$(basename "$FILE")"
  ACTION="$2"
  case "$ACTION" in test|send) ;; *) echo "With a CSV/xlsx file the action is test or send"; exit 2 ;; esac
  ZIP=""; CATEGORY=""
elif [[ $# -eq 3 ]]; then
  ZIP="$1"; CATEGORY="$2"; ACTION="$3"
  case "$ACTION" in test|send|new) ;; *) usage ;; esac
  [[ "$ZIP" =~ ^[0-9]{5}$ ]] || { echo "ZIP must be 5 digits (US)"; exit 2; }
  LIST=""
else
  usage
fi

cd "$(dirname "$0")/.."
[[ -f .venv/bin/activate ]] && source .venv/bin/activate

latest_list() { ls -t recruit/"$ZIP"-"$CATEGORY"-*.csv 2>/dev/null | head -1 || true; }

[[ -z "$LIST" ]] && LIST="$(latest_list)"
if [[ -n "$ZIP" && ( "$ACTION" == "new" || -z "$LIST" ) ]]; then
  echo "== Finding $CATEGORY businesses near $ZIP (checks websites, a few minutes) =="
  python3 scripts/recruit.py find --zip "$ZIP" --category "$CATEGORY" --crawl
  LIST="$(latest_list)"
  echo
  echo "Review the list before sending: open $LIST in Numbers, delete rows you don't want, save as CSV."
  [[ "$ACTION" == "new" ]] && exit 0
  echo
fi
echo "Using list: $LIST"

if [[ "$ACTION" == "test" ]]; then
  python3 scripts/recruit.py send "$LIST" --to "$PREVIEW_TO"
  echo "Check $PREVIEW_TO. When happy, run the same command with send instead of test."
  exit 0
fi

# send: show who would get it, then ask
python3 scripts/recruit.py send "$LIST"
echo
read -r -p "Send these emails to the businesses now? Type yes to send: " ANSWER
if [[ "$ANSWER" == "yes" ]]; then
  python3 scripts/recruit.py send "$LIST" --yes
  python3 scripts/recruit.py status
else
  echo "Nothing sent."
fi
