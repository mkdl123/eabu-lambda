# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Eabu is a local services connector platform (AWS SAM / Python 3.12) that lets consumers post service requests and providers respond across three categories: **Eat** (food), **Live** (household help), and **Move** (transportation). The entire backend is deployed as individual AWS Lambda functions fronted by API Gateway, with DynamoDB as the datastore.

## Commands

```bash
# Build (requires Docker)
sam build --use-container

# First-time deploy (interactive prompts)
sam deploy --guided

# Subsequent deploys
sam deploy

# Invoke a single function locally
sam local invoke <FunctionLogicalName> --event events/event.json

# Start local API on port 3000
sam local start-api

# Run unit tests
pip install -r tests/requirements.txt --user
python -m pytest tests/unit -v

# Run integration tests (requires a deployed stack)
AWS_SAM_STACK_NAME="eabu-lambda" python -m pytest tests/integration -v

# Tail live Lambda logs
sam logs -n <FunctionLogicalName> --stack-name "eabu-lambda" --tail
```

## Architecture

### Directory layout

- `template.yaml` — SAM template defining all Lambda functions and a shared Lambda Layer
- `eabu-dish/` — all Lambda function source code and the shared layer
  - `python/my_utils/helpers.py` — shared utility module (available to every function via the layer)
  - `<function-name>/app.py` — each function's entry point; handler is always `app.lambda_handler`
  - `<function-name>/requirements.txt` — per-function dependencies
- `events/` — sample JSON event payloads for local invocation
- `.aws-sam/build/` — SAM build artifacts (generated, do not edit)

### Shared Lambda Layer

`eabu-dish/python/my_utils/helpers.py` is packaged as the `SharedLayer` and injected into every function. It provides:

- `_parse_body(event)` — decodes base64 bodies if needed, parses JSON
- `_validate_payload(payload, REQUIRED_FIELDS)` — raises `ValueError` on missing fields
- `_response(status, body)` — returns standard API Gateway JSON response with CORS `*` headers
- `increment_counter(partition_key, table)` — atomically increments a counter in DynamoDB and returns the new value; used to generate auto-increment IDs

### ID generation pattern

Every writable table stores a sentinel counter row at `{ID: 100, zipcode: '11111'}`. Before inserting a new record, functions call `helpers.increment_counter(100, table)` to get the next sequential ID. New real items are stored with `ID = int(new_counter_value)`.

### DynamoDB tables (configured via environment variables)

| Env var | Default table name | Purpose |
|---|---|---|
| `EAT_CONSUMER_TABLE_NAME` | `eabu-eat-consumer` | Consumer eat requests |
| `LIVE_CONSUMER_TABLE_NAME` | `eabu-live-consumer` | Consumer live requests |
| `MOVE_CONSUMER_TABLE_NAME` | `eabu-move-consumer` | Consumer move requests |
| `EAT_PROVIDER_TABLE_NAME` | `eabu-eat-provider` | Provider eat services |
| `LIVE_PROVIDER_TABLE_NAME` | `eabu-live-provider` | Provider live services |
| `MOVE_PROVIDER_TABLE_NAME` | `eabu-move-provider` | Provider move services |
| `EAT_OFFER_TABLE_NAME` | `eabu-eat-offer` | Provider offers to eat consumers |
| `LIVE_OFFER_TABLE_NAME` | `eabu-live-offer` | Provider offers to live consumers |
| `MOVE_OFFER_TABLE_NAME` | `eabu-move-offer` | Provider offers to move consumers |
| `EABU_USER_TABLE_NAME` | `eabu-users` | User profiles |
| `EABU_IDENTITY_TABLE_NAME` | `eabu-identity` | Identity verification records |
| `FOLLOWERS_TABLE_NAME` | `eabu-followers` | Follow relationships (pUserId → cEmail) |
| `EAT/LIVE/MOVE_INTERESTED_TABLE_NAME` | `eabu-*-interested` | Interest/bookmark records |
| `EABU_FEEDBACK_TABLE_NAME` | `eabu-feedback` | User feedback submissions |

### S3

- `eabu-identity-pictures` — stores selfie/license images for identity verification; keyed as `{userId}/{selfie|license_front|license_back}.jpg`. The `eabu-user` function generates presigned GET URLs from this bucket.

### Notification pattern

`eabu-notification/app.py` is invoked asynchronously (via `NOTIFICATION_FUNCTION_ARN`) by provider-post functions. It reads follower emails from `eabu-followers` (keyed on `pUserId`) and sends SES emails from `eabu@eabu.net`.

### Function naming conventions

Functions follow a `{role}-{action}-{category}` pattern:
- `provider-post-{eat|live|move}` — provider creates a service listing
- `provider-get-{eat|live|move}` — provider retrieves their listings
- `provider-offer-{eat|live|move}` — provider submits an offer in response to a consumer request
- `provider-my-{eat|live|move}-services` — provider views their own active services
- `consumer-post-{eat|live|move}` — consumer posts a service request
- `consumer-get-{eat|live|move}` — consumer browses available services by zipcode
- `consumer-{eat|live|move}-offers` — consumer views offers received

### Get functions: query pattern

GET functions query DynamoDB by `zipcode` (partition key) + `ID > 0`, filtered to records created within the last 30 days. When a `userId` path parameter is not `"NA"`, they instead query a `userId-index` GSI to return only that user's items.
