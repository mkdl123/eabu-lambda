#!/usr/bin/env bash
# Add routes to the hand-managed REST API behind api.eabu.net (Lifestyle, profile
# picture, listing picture and favourites routes). Each route gets the Cognito authorizer, a Lambda proxy integration and
# a mock OPTIONS for CORS, like the existing routes. --apply also removes the methods in
# RETIRED_ROUTES (the old identity/selfie upload, the
# old follow feature, replaced by favourites, and two lookups no app version called).
#
#   ./scripts/create-api-routes.sh            # report only, changes nothing
#   ./scripts/create-api-routes.sh --apply    # create missing routes (not live yet)
#   ./scripts/create-api-routes.sh --deploy   # publish the API to the stage
#
# Run --apply AFTER `sam deploy`, because it needs the functions. Routes that already
# exist are left alone, so both steps are safe to re-run. --deploy publishes EVERY pending change on this API, not only these routes,
# so check for other console edits first. It prints the command to roll back.
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
API_ID="${API_ID:-wrc00h65j4}"
STAGE="${STAGE:-dev}"          # api.eabu.net maps to this stage
STACK="${STACK:-eabulambda}"
AUTHORIZER_NAME="${AUTHORIZER_NAME:-EabuCognitoAuthorizer}"
MODE="${1:-report}"
case "$MODE" in report|--apply|--deploy) ;; *) echo "usage: $0 [--apply|--deploy]" >&2; exit 2 ;; esac

api() { aws apigateway --region "$REGION" "$@"; }

if [[ "$MODE" == "--deploy" ]]; then
  previous=$(api get-stage --rest-api-id "$API_ID" --stage-name "$STAGE" --query deploymentId --output text)
  current=$(api create-deployment --rest-api-id "$API_ID" --stage-name "$STAGE" \
    --description "${DEPLOY_DESCRIPTION:-Add API routes}" --query id --output text)
  echo "Deployed $current to $STAGE (was $previous). To roll back:"
  echo "  aws apigateway update-stage --region $REGION --rest-api-id $API_ID --stage-name $STAGE \\"
  echo "    --patch-operations op=replace,path=/deploymentId,value=$previous"
  exit 0
fi

# method  path  function logical id
ROUTES=(
  "POST /eabu-lifestyle ProviderPostLifestyleFunction"
  "GET /eabu-lifestyle/{zip} ConsumerGetLifestyleFunction"
  "POST /eabu-lifestyle-consumer ConsumerPostLifestyleFunction"
  "GET /eabu-lifestyle-consumer/{zip}/{userId} ProviderGetLifestyleFunction"
  "POST /offer-lifestyle LifestyleOfferFunction"
  "GET /offer-lifestyle/{userId}/{itemId} ConsumerReceivedLifestyleOfferFunction"
  "GET /provider-my-lifestyle-services/{userId} ProviderMyLifestyleServicesFunction"
  "POST /profile-picture EabuProfilePictureFunction"
  "DELETE /profile-picture/{userId} EabuProfilePictureFunction"
  "POST /listing-picture EabuListingPictureFunction"
  "DELETE /listing-picture/{category}/{zipcode}/{itemId}/{slot} EabuListingPictureFunction"
  "GET /favourites/{userId}/{kind} EabuFavouritesFunction"
  "POST /favourites EabuFavouritesFunction"
  "DELETE /favourites/{userId}/{kind}/{favUserId} EabuFavouritesFunction"
  "GET /favourites-feed/{userId}/{kind} EabuFavouritesFunction"
)

# method  path  - methods to remove (their function no longer exists)
RETIRED_ROUTES=(
  "POST /identity"
  "DELETE /identity/{userId}"
  "POST /eabu-follow"
  "GET /eabu-i-follow/{cUserId}"
  "POST /eabu-user-zip"
  "GET /eabu-get-user/{param}"
)

resource_id() { # path -> id, or empty when the resource does not exist
  local id
  id=$(api get-resources --rest-api-id "$API_ID" --limit 500 \
    --query "items[?path=='$1'].id | [0]" --output text)
  [[ "$id" == "None" ]] || echo "$id"
}

ensure_resource() { # path -> id, creating each missing segment
  local path="$1" parent="" built="" part id
  parent=$(resource_id /)
  IFS=/ read -ra parts <<< "${path#/}"
  for part in "${parts[@]}"; do
    built="$built/$part"
    id=$(resource_id "$built")
    if [[ -z "$id" ]]; then
      id=$(api create-resource --rest-api-id "$API_ID" --parent-id "$parent" \
        --path-part "$part" --query id --output text)
    fi
    parent="$id"
  done
  echo "$parent"
}

has_method() { # resource id, http method. A method with no integration is unfinished.
  local type
  type=$(api get-method --rest-api-id "$API_ID" --resource-id "$1" --http-method "$2" \
    --query methodIntegration.type --output text 2>/dev/null) || return 1
  [[ "$type" != "None" ]]
}

clear_unfinished() { # resource id, http method. Removes what an interrupted run left behind.
  api delete-method --rest-api-id "$API_ID" --resource-id "$1" --http-method "$2" >/dev/null 2>&1 || true
}

function_name() { # logical id -> deployed name, or a failure when the stack does not have it
  aws cloudformation describe-stack-resource --region "$REGION" --stack-name "$STACK" \
    --logical-resource-id "$1" --query StackResourceDetail.PhysicalResourceId --output text 2>/dev/null
}

ensure_options() { # resource id
  local id="$1" prefix=method.response.header.Access-Control-Allow
  has_method "$id" OPTIONS && return
  clear_unfinished "$id" OPTIONS
  api put-method --rest-api-id "$API_ID" --resource-id "$id" --http-method OPTIONS \
    --authorization-type NONE >/dev/null
  api put-integration --rest-api-id "$API_ID" --resource-id "$id" --http-method OPTIONS \
    --type MOCK --request-templates '{"application/json":"{\"statusCode\": 200}"}' >/dev/null
  api put-method-response --rest-api-id "$API_ID" --resource-id "$id" --http-method OPTIONS \
    --status-code 200 --response-parameters \
    "{\"$prefix-Headers\":true,\"$prefix-Methods\":true,\"$prefix-Origin\":true}" >/dev/null
  api put-integration-response --rest-api-id "$API_ID" --resource-id "$id" --http-method OPTIONS \
    --status-code 200 --response-parameters \
    "{\"$prefix-Headers\":\"'Content-Type,Authorization,X-User-Id,X-Api-Key,X-Amz-Date,X-Amz-Security-Token,X-Amz-User-Agent'\",\"$prefix-Methods\":\"'GET,POST,PUT,DELETE,OPTIONS'\",\"$prefix-Origin\":\"'*'\"}" >/dev/null
}

path_params() { # path -> JSON map of its {params}, or empty when it has none
  local params
  params=$(grep -oE '\{[^}]+\}' <<< "$1" | tr -d '{}' | sed 's/.*/"method.request.path.&":true/' | paste -sd, - || true)
  [[ -z "$params" ]] || echo "{$params}"
}

if [[ "$MODE" == "report" ]]; then
  for route in "${ROUTES[@]}"; do
    read -r method path _ <<< "$route"
    id=$(resource_id "$path")
    if [[ -n "$id" ]] && has_method "$id" "$method"; then echo "ok       $method $path"
    else echo "missing  $method $path"; fi
  done
  for route in "${RETIRED_ROUTES[@]}"; do
    read -r method path <<< "$route"
    id=$(resource_id "$path")
    if [[ -n "$id" ]] && has_method "$id" "$method"; then echo "retire   $method $path"
    else echo "gone     $method $path"; fi
  done
  echo "Report only. Re-run with --apply to create missing routes and remove retired ones."
  exit 0
fi

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
AUTHORIZER_ID=$(api get-authorizers --rest-api-id "$API_ID" \
  --query "items[?name=='$AUTHORIZER_NAME'].id | [0]" --output text)
[[ "$AUTHORIZER_ID" != "None" ]] || { echo "Authorizer $AUTHORIZER_NAME not found" >&2; exit 1; }

# Resolve every function first, so a missing `sam deploy` stops before anything is created.
for route in "${ROUTES[@]}"; do
  read -r _ _ logical <<< "$route"
  function_name "$logical" >/dev/null || { echo "Function $logical not found. Run sam deploy first." >&2; exit 1; }
done

for route in "${ROUTES[@]}"; do
  read -r method path logical <<< "$route"
  name=$(function_name "$logical")
  id=$(ensure_resource "$path")
  ensure_options "$id"
  if has_method "$id" "$method"; then echo "ok       $method $path"; continue; fi
  clear_unfinished "$id" "$method"

  params=$(path_params "$path")
  api put-method --rest-api-id "$API_ID" --resource-id "$id" --http-method "$method" \
    --authorization-type COGNITO_USER_POOLS --authorizer-id "$AUTHORIZER_ID" \
    ${params:+--request-parameters "$params"} >/dev/null
  arn="arn:aws:lambda:$REGION:$ACCOUNT:function:$name"
  api put-integration --rest-api-id "$API_ID" --resource-id "$id" --http-method "$method" \
    --type AWS_PROXY --integration-http-method POST --content-handling CONVERT_TO_TEXT \
    --uri "arn:aws:apigateway:$REGION:lambda:path/2015-03-31/functions/$arn/invocations" >/dev/null
  api put-method-response --rest-api-id "$API_ID" --resource-id "$id" --http-method "$method" \
    --status-code 200 >/dev/null

  statement="apigw-$(tr '[:upper:]' '[:lower:]' <<< "$method")$(tr -d '{}' <<< "$path" | tr / -)"
  aws lambda add-permission --region "$REGION" --function-name "$name" --statement-id "$statement" \
    --action lambda:InvokeFunction --principal apigateway.amazonaws.com \
    --source-arn "arn:aws:execute-api:$REGION:$ACCOUNT:$API_ID/*/$method$(sed -E 's/\{[^}]+\}/*/g' <<< "$path")" >/dev/null
  echo "created  $method $path"
done

for route in "${RETIRED_ROUTES[@]}"; do
  read -r method path <<< "$route"
  id=$(resource_id "$path")
  if [[ -n "$id" ]] && has_method "$id" "$method"; then
    api delete-method --rest-api-id "$API_ID" --resource-id "$id" --http-method "$method" >/dev/null
    echo "removed  $method $path"
  else
    echo "gone     $method $path"
  fi
done

echo "Changes are not live yet. Run $0 --deploy to publish them."
