#!/usr/bin/env bash
# Kill switch: stop (and restart) all eabu traffic with one command. Nothing is deleted.
#
#   scripts/kill-switch.sh status                       # what is on/off right now (changes nothing)
#   scripts/kill-switch.sh pause ["message"] [--yes]    # stop everything
#   scripts/kill-switch.sh resume [--yes]               # put everything back as it was
#   add --dry-run to pause/resume to print the changes without making them
#
# pause, in this order:
#   1. SSM /eabu/app-status = {"paused": true, "message": ...}: the app (2.2+) shows the message
#      instead of its screens (GET /guest/status, function EabuAppStatusFunction).
#   2. API Gateway stage throttle 0/0: every route answers 429 within seconds, EXCEPT
#      GET /guest/status, which gets its own small throttle so the message still loads.
#   3. Reserved concurrency 0 on every Lambda in the stack except EabuAppStatusFunction: also stops
#      what doesn't come through the API (Cognito sign-up trigger, async Discover runs).
#   4. Disables the EventBridge schedules (weekly Discover, daily ad reminder).
# resume undoes these in reverse order, from what pause saved in SSM /eabu/kill-switch-state.
#
# Not covered (see the shut-down notes in CLAUDE.md): Apple keeps renewing paid-ad subscriptions,
# the website (Cloudflare) stays up, and Cognito sign-in with a User Id still works (the API doesn't).
# Don't `sam deploy` while paused: a deploy that changes a schedule turns it back on.
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
API_ID="${API_ID:-wrc00h65j4}"
STAGE="${STAGE:-dev}"
STACK="${STACK:-eabulambda}"
STATUS_FUNCTION="EabuAppStatusFunction"      # logical id of the one function left running
STATUS_PATH="/~1guest~1status/GET"           # method-settings path of GET /guest/status
STATUS_RATE="${STATUS_RATE:-20}"             # its throttle while paused (requests/s)
STATUS_BURST="${STATUS_BURST:-40}"
APP_STATUS_PARAM="/eabu/app-status"
STATE_PARAM="/eabu/kill-switch-state"
DEFAULT_RATE=50 DEFAULT_BURST=100            # used by resume only if the saved state is missing
DEFAULT_MESSAGE="eabu is paused for maintenance. Please check back soon."

export AWS_RETRY_MODE=adaptive AWS_MAX_ATTEMPTS=10
aws_() { aws --region "$REGION" "$@"; }

CMD="${1:-}"; shift || true
MESSAGE="" YES=0 DRY=0
for a in "$@"; do
  case "$a" in
    --yes) YES=1 ;;
    --dry-run) DRY=1 ;;
    --*) echo "unknown option $a" >&2; exit 2 ;;
    *) MESSAGE="$a" ;;
  esac
done

run() { # mutating call: printed in --dry-run, executed otherwise
  if (( DRY )); then echo "  [dry-run] aws $*"; else aws_ "$@" >/dev/null; fi
}

confirm() {
  (( YES || DRY )) && return 0
  read -r -p "$1 Type yes to continue: " ans
  [[ "$ans" == "yes" ]] || { echo "Cancelled."; exit 1; }
}

stack_functions() { # physical names of every Lambda in the stack except the status function
  aws_ cloudformation list-stack-resources --stack-name "$STACK" --output text \
    --query "StackResourceSummaries[?ResourceType=='AWS::Lambda::Function' && LogicalResourceId!='$STATUS_FUNCTION'].PhysicalResourceId"
}

status_function() {
  aws_ cloudformation list-stack-resources --stack-name "$STACK" --output text \
    --query "StackResourceSummaries[?LogicalResourceId=='$STATUS_FUNCTION'].PhysicalResourceId"
}

stack_schedules() {
  aws_ cloudformation list-stack-resources --stack-name "$STACK" --output text \
    --query "StackResourceSummaries[?ResourceType=='AWS::Scheduler::Schedule'].PhysicalResourceId"
}

schedule_name() { echo "${1##*|}"; }        # physical id may be "group|name"
schedule_group() { [[ "$1" == *"|"* ]] && echo "${1%%|*}" || echo default; }

set_schedule_state() { # schedule-physical-id ENABLED|DISABLED
  local name group json
  name=$(schedule_name "$1"); group=$(schedule_group "$1")
  json=$(aws_ scheduler get-schedule --name "$name" --group-name "$group" --output json)
  json=$(jq -c --arg s "$2" 'del(.Arn, .CreationDate, .LastModificationDate) | .State = $s' <<< "$json")
  if (( DRY )); then echo "  [dry-run] aws scheduler update-schedule $name -> $2"
  else aws_ scheduler update-schedule --cli-input-json "$json" >/dev/null; fi
}

stage_throttle() { # prints "rate burst" of the stage-wide */* setting
  aws_ apigateway get-stage --rest-api-id "$API_ID" --stage-name "$STAGE" --output text \
    --query 'methodSettings."*/*".[throttlingRateLimit, throttlingBurstLimit]'
}

get_param() { aws_ ssm get-parameter --name "$1" --query Parameter.Value --output text 2>/dev/null || true; }

route_exists() {
  aws_ apigateway get-resources --rest-api-id "$API_ID" --limit 500 --output text \
    --query "items[?path=='/guest/status'].resourceMethods.GET" 2>/dev/null | grep -q .
}

# ---------------------------------------------------------------- status
do_status() {
  local app throttle rate burst srate fns paused=0 total=0 s state
  app=$(get_param "$APP_STATUS_PARAM")
  echo "App message ($APP_STATUS_PARAM): ${app:-<not set: not paused>}"

  read -r rate burst <<< "$(stage_throttle)"
  srate=$(aws_ apigateway get-stage --rest-api-id "$API_ID" --stage-name "$STAGE" --output text \
    --query 'methodSettings."~1guest~1status/GET".throttlingRateLimit')
  echo "API stage $STAGE throttle: rate $rate, burst $burst  (/guest/status: ${srate/None/stage default})"

  fns=$(stack_functions)
  for f in $fns; do
    total=$((total + 1))
    c=$(aws_ lambda get-function-concurrency --function-name "$f" --query ReservedConcurrentExecutions --output text)
    [[ "$c" == "0" ]] && paused=$((paused + 1))
  done
  echo "Lambdas stopped (reserved concurrency 0): $paused of $total  ($STATUS_FUNCTION always runs)"

  for s in $(stack_schedules); do
    state=$(aws_ scheduler get-schedule --name "$(schedule_name "$s")" --group-name "$(schedule_group "$s")" --query State --output text)
    echo "Schedule $(schedule_name "$s"): $state"
  done

  [[ -n "$(get_param "$STATE_PARAM")" ]] && echo "Saved state: yes ($STATE_PARAM)" || echo "Saved state: none"

  if [[ "$rate" == "0.0" || "$rate" == "0" ]] && (( paused == total )); then echo "=> PAUSED"
  elif [[ "$rate" != "0.0" && "$rate" != "0" ]] && (( paused == 0 )); then echo "=> RUNNING"
  else echo "=> PARTLY PAUSED: run pause or resume again to finish"; fi
}

# ---------------------------------------------------------------- pause
do_pause() {
  local msg="${MESSAGE:-$DEFAULT_MESSAGE}" saved rate burst fns reserved="{}" scheds="{}" f c s st app_json
  [[ -n "$(status_function)" ]] || echo "WARNING: $STATUS_FUNCTION isn't deployed: the app will show errors, not a message."
  route_exists || echo "WARNING: GET /guest/status isn't on the API (scripts/create-api-routes.sh --apply/--deploy): the app will show errors, not a message."
  confirm "This stops ALL eabu traffic (app, guests, Apple notifications, share pages, schedules)."

  saved=$(get_param "$STATE_PARAM")
  fns=$(stack_functions)
  if [[ -n "$saved" ]]; then
    echo "Already paused once (saved state kept, not overwritten); re-applying everything."
  else
    echo "Saving current settings to $STATE_PARAM ..."
    read -r rate burst <<< "$(stage_throttle)"
    for f in $fns; do   # remember any reserved concurrency set on purpose (none today)
      c=$(aws_ lambda get-function-concurrency --function-name "$f" --query ReservedConcurrentExecutions --output text)
      [[ "$c" != "None" && "$c" != "0" ]] && reserved=$(jq -c --arg f "$f" --argjson c "$c" '.[$f] = $c' <<< "$reserved")
    done
    for s in $(stack_schedules); do
      st=$(aws_ scheduler get-schedule --name "$(schedule_name "$s")" --group-name "$(schedule_group "$s")" --query State --output text)
      scheds=$(jq -c --arg s "$s" --arg st "$st" '.[$s] = $st' <<< "$scheds")
    done
    saved=$(jq -nc --arg r "$rate" --arg b "$burst" --argjson res "$reserved" --argjson sch "$scheds" \
      --arg at "$(date -u +%FT%TZ)" '{pausedAt: $at, rate: ($r|tonumber), burst: ($b|tonumber), reserved: $res, schedules: $sch}')
    run ssm put-parameter --name "$STATE_PARAM" --type String --overwrite --value "$saved"
  fi

  echo "1/4 App message on"
  app_json=$(jq -nc --arg m "$msg" '{paused: true, message: $m}')
  run ssm put-parameter --name "$APP_STATUS_PARAM" --type String --overwrite --value "$app_json"

  echo "2/4 API throttled to 0 (except GET /guest/status at $STATUS_RATE/s)"
  run apigateway update-stage --rest-api-id "$API_ID" --stage-name "$STAGE" --patch-operations \
    "op=replace,path=$STATUS_PATH/throttling/rateLimit,value=$STATUS_RATE" \
    "op=replace,path=$STATUS_PATH/throttling/burstLimit,value=$STATUS_BURST" \
    "op=replace,path=/*/*/throttling/rateLimit,value=0" \
    "op=replace,path=/*/*/throttling/burstLimit,value=0"

  echo "3/4 Stopping Lambdas"
  for f in $fns; do run lambda put-function-concurrency --function-name "$f" --reserved-concurrent-executions 0; done

  echo "4/4 Disabling schedules"
  for s in $(stack_schedules); do set_schedule_state "$s" DISABLED; done

  (( DRY )) && echo "Dry run: nothing changed." || echo "PAUSED. Undo with: scripts/kill-switch.sh resume"
}

# ---------------------------------------------------------------- resume
do_resume() {
  local saved rate burst fns f c s st
  confirm "This turns eabu traffic back on."
  saved=$(get_param "$STATE_PARAM")
  if [[ -z "$saved" ]]; then
    echo "No saved state: restoring defaults (throttle $DEFAULT_RATE/$DEFAULT_BURST, no Lambda limits, schedules on)."
    saved=$(jq -nc --argjson r $DEFAULT_RATE --argjson b $DEFAULT_BURST '{rate: $r, burst: $b, reserved: {}, schedules: {}}')
  fi
  rate=$(jq -r .rate <<< "$saved"); burst=$(jq -r .burst <<< "$saved")
  [[ "$rate" == "0" || "$rate" == "0.0" ]] && rate=$DEFAULT_RATE    # never "restore" a pause
  [[ "$burst" == "0" ]] && burst=$DEFAULT_BURST

  echo "1/4 Enabling schedules"
  for s in $(stack_schedules); do
    st=$(jq -r --arg s "$s" '.schedules[$s] // "ENABLED"' <<< "$saved")
    set_schedule_state "$s" "$st"
  done

  echo "2/4 Starting Lambdas"
  fns=$(stack_functions)
  for f in $fns; do
    c=$(jq -r --arg f "$f" '.reserved[$f] // empty' <<< "$saved")
    if [[ -n "$c" ]]; then run lambda put-function-concurrency --function-name "$f" --reserved-concurrent-executions "$c"
    else run lambda delete-function-concurrency --function-name "$f"; fi
  done

  echo "3/4 API throttle back to $rate/$burst"
  run apigateway update-stage --rest-api-id "$API_ID" --stage-name "$STAGE" --patch-operations \
    "op=replace,path=/*/*/throttling/rateLimit,value=$rate" \
    "op=replace,path=/*/*/throttling/burstLimit,value=$burst"
  if aws_ apigateway get-stage --rest-api-id "$API_ID" --stage-name "$STAGE" --output text \
       --query 'methodSettings."~1guest~1status/GET"' | grep -qv '^None$'; then
    run apigateway update-stage --rest-api-id "$API_ID" --stage-name "$STAGE" --patch-operations "op=remove,path=$STATUS_PATH"
  fi

  echo "4/4 App message off"
  run ssm put-parameter --name "$APP_STATUS_PARAM" --type String --overwrite --value '{"paused": false}'
  if [[ -n "$(get_param "$STATE_PARAM")" ]]; then run ssm delete-parameter --name "$STATE_PARAM"; fi

  (( DRY )) && echo "Dry run: nothing changed." || echo "RESUMED. Check with: scripts/kill-switch.sh status"
}

case "$CMD" in
  status) do_status ;;
  pause) do_pause ;;
  resume) do_resume ;;
  *) echo "usage: $0 status | pause [\"message\"] [--yes] [--dry-run] | resume [--yes] [--dry-run]" >&2; exit 2 ;;
esac
