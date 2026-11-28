# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Eabu is a local services connector platform (AWS SAM / Python 3.12) that lets consumers post service requests and providers respond across four categories: **Eat** (food), **Live** (household help), **Move** (transportation), and **Lifestyle** (fitness, wellness, beauty). The entire backend is deployed as individual AWS Lambda functions fronted by API Gateway, with DynamoDB as the datastore.

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
| `LIFESTYLE_{CONSUMER,PROVIDER,OFFER,INTERESTED,RATED}_TABLE_NAME` | `eabu-lifestyle-*` | Lifestyle requests, services, offers, interest and ratings |
| `EABU_IDENTITY_TABLE_NAME` | `eabu-identity` | Identity verification records |
| `FAVOURITES_TABLE_NAME` | `eabu-favourites` | Favourite sellers/buyers (userId + `kind#favUserId`) |
| `EAT/LIVE/MOVE_INTERESTED_TABLE_NAME` | `eabu-*-interested` | Interest/bookmark records |
| `EABU_FEEDBACK_TABLE_NAME` | `eabu-feedback` | User feedback submissions |

Tables are created outside the SAM stack (for Lifestyle: `scripts/create-lifestyle-tables.sh`).

### API Gateway

The REST API behind `api.eabu.net` is managed outside `template.yaml`, so a new route needs setting up there as well as its function (`scripts/create-api-routes.sh` adds them).

### Logs and cleanup

Functions run on arm64 (Graviton, 20% cheaper) with a 10-second timeout; the two picture functions have 30. Lambda log groups keep 30 days. Don't add `print()` debugging; use `logger`, and never log bodies or PII. A new function's log group starts with no expiry, so run `scripts/set-log-retention.sh` after deploying one. The SAM deploy bucket deletes files after 90 days (a lifecycle rule; 90 rather than 30 so a CloudFormation rollback can still find the previous deploy's code). On 2026-11-10 the cost/cleanup pass deleted the WAF and both CloudFront distributions, the old Amplify environments `d87d3`/`cf209` (the app uses Amplify app `d25vnfgn4l0ej3`, env `c88ad`), the `helloworld` Lambda and the empty `eabu-http` HTTP API.

### App opens by ZIP (2026-11-26)

`python3 scripts/opens.py [--days 30] [--country US|IN]` shows where the app is opened: per UTC day and country, and per ZIP/PIN with an approximate state. The first page of the nearby feed and `/guest/feed` (not `?map=1`, no `?cursor`) calls `helpers.count_open`, which keeps `opens#YYYY-MM-DD#<zip>` rows in `eabu-rate-limit` (`userOpens`/`guestOpens`, `userPeople`/`guestPeople`; 400-day TTL). People are counted with `opener#...` marker rows keyed by a hash of day + User Id or guest IP (2-day TTL); no User Id, IP or position is stored. It never fails the feed. Tests: `tests/unit/test_opens.py`.

### Kill switch (pause all traffic)

`scripts/kill-switch.sh status | pause ["message"] | resume` (`--yes` skips the prompt, `--dry-run` prints the changes). `pause` sets SSM `/eabu/app-status` to `{"paused": true, "message"}`, throttles the `dev` stage to 0/0 (every route 429) except `GET /guest/status` (20/s), sets reserved concurrency 0 on every stack Lambda except `EabuAppStatusFunction`, and disables the two EventBridge schedules. It saves the previous settings in SSM `/eabu/kill-switch-state`; `resume` restores them in reverse order and deletes it. Nothing is deleted. `GET /guest/status` (`eabu-app-status`, public) is the route that stays up so the app (2.2+, `components/MaintenanceGate.js`) can show the message instead of errors; it answers `{"paused": false}` on any error and caches for 30 s. Don't `sam deploy` while paused. Not covered: Apple keeps renewing paid-ad subscriptions (remove the product from sale), the Cloudflare website stays up, and Cognito sign-in with a User Id still works. Tested 2026-11-25 on a temporary stage (0/0 gives 429, the per-route override stays 200) and on one function (concurrency 0 rejects invokes).

### S3

- `eabu-identity-pictures` — despite the name, now stores only each user's optional profile picture at `{userId}/profile.jpg` (one per user; the bucket is unversioned, so a new upload replaces the old one). `eabu-profile-picture` handles `POST /profile-picture` (upload/replace) and `DELETE /profile-picture/{userId}`; no identity images (selfies, licences) are collected. The `eabu-user` function generates presigned GET URLs from this bucket.

### Favourites

`eabu-favourites` lets buyers save sellers (kind `seller`) and sellers save buyers (kind `buyer`), up to 50 each. `GET /favourites-feed/{userId}/{kind}?zip=&nearby=1` (used by the app's Home screen) returns everyone's posts of that kind from the last 30 days within 50 miles of the ZIP, except the user's own, newest first, each with `distanceMiles` and `isFavourite`. It scans the post tables, which is fine while they're small (TTL bounds them to 60 days of posts); a warm container reuses each scan for `SCAN_CACHE_SECONDS` (60), so scrolling pages don't rescan. Switch to a geohash GSI if they grow. Without `nearby` it returns only the favourites' posts (within 50 miles when `?zip` is given). Both modes page with `?limit=N` (1-50) and `?cursor=` (the previous response's `nextCursor`, null at the end; the app's Home loads 10 at a time as the user scrolls). The cursor is the last post's (createdAt, category, ID), so new posts don't shift later pages; each page still scans, but pictures are only presigned for the page. Without `limit` the full list comes back (older app builds). `?map=1` (either mode, for the app's Home Map view, 1.5) returns only posts with an address pin, unpaged, up to `MAP_MAX_POSTS` (500). The pin is `addressLat`/`addressLng`, which the app looks up from the address when posting (`helpers.address_location`; saved only with an address, never rounded, unlike the phone's `latitude`/`longitude`). `GET /eabu-ad/{status}?zip=` limits ads to advertisers within 50 miles. Distances come from `helpers.zip_distance_miles` / `helpers.within_miles` (`helpers.NEARBY_MILES`), which use the Census ZIP centroids bundled in the layer (`python/my_utils/zip_centroids.csv`); an unknown or non-US code only matches itself. The app mixes the ads into the list at random positions and highlights favourites. Favourites replaced the old follow feature: its code, the `eabu-notification` emails and the `/eabu-follow` routes were removed; the `eabu-followers` table was deleted on 2026-11-08.

### Guest browsing (1.9)

People can browse before creating an account: the app gets the ZIP from the phone's location (or typed) and shows the Buy-mode Home. Three public routes (no Cognito authorizer, `PUBLIC_ROUTES`) serve it: `GET /guest/feed?zip=&kind=seller|buyer&limit=&cursor=&map=1` (`eabu-favourites`, same posts as the nearby feed but without `phone`, `email` and `address`, phone numbers and emails in title/description replaced by `[sign up to see]`, pins rounded to 2 decimals, `contactHidden: true`), `GET /guest/discovered?zip=&v=2` (`eabu-discovered`; guests may start a new area within the daily cap unless `GUEST_CAN_START_AREAS=false`) and `GET /guest/ads?zip=` (`eabu-advertisement-get`, the active ads). `helpers.is_guest_route` tells them apart; `helpers.rate_limited` counts guests per IP address (`GUEST_LIMIT_PER_HOUR`, 1200, higher because carriers share addresses). `GET /guest/demand?zip=` (`eabu-favourites`, public) returns only counts of buyer requests within 50 miles in the last 7 days (`requests`, `byCategory`) for the guest Home "Earn money" card (1.9.2). Contacting a seller, posting, offers and everything else still need an account. Tests: `tests/unit/test_guest.py`. Guests' "Use my location" turns the phone's position into a ZIP with Amazon Location (`Geo.searchByCoordinates` in the app), which needs AWS credentials: since 2026-11-22 the Cognito identity pool (`us-east-1:53cd420f-...`) allows unauthenticated identities, and the guest role `amplify-eabu-dev-c88ad-unauthRole` has one inline policy, `GuestZipLookupOnly` (`geo:SearchPlaceIndexForPosition` on `placeIndex43161903-dev`, nothing else). Since 2026-11-25 the guest role has NO policy (`GuestZipLookupOnly` removed: guest credentials could call Amazon Location directly, bypassing every API limit; to restore, put that inline policy back on the role), so the Amazon fallback fails and guests type their ZIP. Since 1.9.1 (61) the app first asks `GET /guest/zip?lat=&lng=` (`eabu-favourites`, public, `helpers.nearest_zip`: nearest Census ZIP centroid within 30 miles, else 404; the app sends the position rounded to 3 decimals, never stored or logged) and only falls back to Amazon Location. Amazon Location for guests also needs `allowGuestAccess: true` in the app's Amplify config (1.9.1). To undo the guest identities: set `AllowUnauthenticatedIdentities` false.

### India (2.0)

Postal codes are US ZIPs (5 digits) or India PIN codes (6 digits, first digit 1-9); `helpers._norm_zip` accepts both (`"560 001"` becomes `560001`) and `helpers.country_of_zip` tells them apart, so they never collide and no country field is stored. Distances use both bundled centroid files (`zip_centroids.csv`, US Census; `in_pin_centroids.csv`, GeoNames CC BY 4.0, district-level precision, built from the post-office list as the median per PIN). Because the PIN centroids are coarse, `/guest/zip` asks Amazon Location (`helpers.india_pin_at`, place index `placeIndex43161903-dev`, single use, never stored) for positions in India and falls back to the nearest centroid. Discovered asks Claude for "PIN code X, India" (Google Maps, Zomato and Justdial ratings count) and skips the US-only Census geocoder (no map pins in India) and Ticketmaster. The content filter adds Hindi/Hinglish terms (Latin letters and Devanagari). Still US-only: the free-post month resets at US Eastern time. Tests: `tests/unit/test_india.py`.

### Sign in with email (2.1)

The user pool `us-east-1_G0VobzUMd` is username-only, and email can't be added as a sign-in alias to an existing pool, so `eabu-email-sign-in` swaps an email for the User Id: `POST /guest/sign-in-name {email, password}` finds the account by email (`ListUsers` filter, case-insensitive in real Cognito) and checks the password with `AdminInitiateAuth` on the server-only app client `SignInClientId` (`eabu-server-email-sign-in`, `ALLOW_ADMIN_USER_PASSWORD_AUTH`, 5-minute tokens, thrown away); it returns `{userId}` only when the password is right, and the same 400 `BAD_CREDENTIALS` for an unknown email or a wrong password. The app then signs in with the User Id as before. `POST /guest/forgot-password {email}` (always the same 200) and `POST /guest/reset-password {email, code, password}` do password reset by email. All three are public, limited per email (`SIGN_IN_TRIES_PER_HOUR`, 20) and per IP (60) in `eabu-rate-limit`; emails and passwords are never logged. `eabu-pre-signup` refuses a new sign-up whose email another account already uses, so an email names one account. The User Id stays the public name and the key everywhere. Tests: `tests/unit/test_email_sign_in.py`.

### Report and block (App Store guideline 1.2)

Every listing, request, offer, ad and profile in the app has a ⋯ button with *Report* and *Block*. `POST /report` (`eabu-report`) saves the report in `eabu-feedback` (`type: report`, `status: open`) and emails it to `ReportEmail` (template parameter, `admin@eabu.net`; SES has production access since 2026-11-19, so any address works). A failed email doesn't fail the report. Apple expects reports to be acted on within 24 hours: remove the content and, if needed, the account, with `scripts/moderate.py` (`reports`, `user`, `remove-post`, `ban`/`unban`, `resolve`; changes need `--yes`). Its `unblock-email <email>` lifts the 30-day sign-up block a deleted account leaves on its email (admin-only override; the rule itself is unchanged). Blocks are rows in `eabu-favourites` with kind `blocked` (up to 500, same `/favourites` routes). Both Home feeds leave out blocked users' posts, and the app also hides them from the category lists, offers and profiles. Ads have no public owner, so a reported ad is only hidden on that device for the session. Deleting an account also deletes the reports that user made.

### Sexual content filter

`my_utils/content_filter.py` keeps sexual content out before it's saved (Report/Block is the backstop for anything it misses):

- **Text**: `helpers._validate_payload` rejects any post, offer, ad or repost whose `content_filter.TEXT_FIELDS` contain a blocked word or phrase (400, the message names the field, never the word). Matching is whole-word after undoing look-alikes (`s3x`, `p0rn`), so "Sussex" passes; words real listings use (nude lipstick, naked cake, kinky twist) are deliberately not blocked. The Cognito pre sign-up trigger also refuses User Ids with blocked words.
- **Pictures**: `eabu-listing-picture` and `eabu-profile-picture` send each picture to Rekognition `DetectModerationLabels` before `put_object`. Nudity or sexual content gets 400 `IMAGE_REJECTED` and isn't stored; swimwear, bare backs and kissing are allowed. If Rekognition fails, the upload is refused with 503 `IMAGE_CHECK_FAILED` (never stored unchecked). About $1 per 1,000 pictures.
- Tune the word lists and `BLOCKED_PICTURE_LABELS` in that file; tests are in `tests/unit/test_content_filter.py` (Rekognition is faked in `tests/unit/conftest.py`, since moto doesn't support it).

### Paid ads (Apple in-app subscription)

Advertisers pay $9.99 for 3 months through an auto-renewing App Store subscription (product `net.eabu.app.ad.quarterly`, template parameter `AdProductId`). Rules: one paid ad per owner, live as soon as Apple confirms payment, shown to every user in the same country as the ad's ZIP/PIN (US or India), whatever their ZIP. Free ads still need approval and only show within 50 miles. Each owner's **first** ad is free: `POST /eabu-advertisement` with `"paid": true` saves it `active` with `freeAd: true`, `autoRenew: false` and `paidThrough` 90 days out (`apple_iap.FREE_AD_DAYS`), skipping the purchase (the response has `free: true`). `apple_iap.first_ad_free` decides: no earlier ad of the owner went live (`freeAd`, `activatedAt` or `originalTransactionId`). `GET /eabu-ad-purchase/{userId}` returns `firstAdFree` so the app can say so. A re-created account doesn't get a second one: marker rows in the ad table (`zipcode` = `freead#u#<sha256 of User Id>` / `freead#e#<sha256 of account email>`, `ID` 0, no `ownerUserId`) are written when a free ad is granted and when `eabu-delete-user-info` deletes an account that had a live ad, and `apple_iap.free_ad_used_before` checks them (the email comes from the ID token's `email` claim). They are never deleted. The flow:

1. `POST /eabu-advertisement` with `"paid": true` saves the ad as `awaiting_payment`, with `ownerUserId`, `paid` and `scope: national`. It returns `ID`, `zipcode`, `productId` and `appAccountToken`.
2. The app buys the subscription with StoreKit, passing `appAccountToken`, a UUID derived from the user by `apple_iap.account_token`.
3. `POST /eabu-ad-purchase` receives the signed transaction, verifies it (`my_utils/apple_iap.py`, Apple root certs in `my_utils/apple_certs/`) and checks the token matches the caller. It then sets the ad `active` with `paidThrough` (epoch seconds) and `originalTransactionId`. With no `ID` it acts as a restore. `GET /eabu-ad-purchase/{userId}` returns the caller's ad.
4. Apple posts renewals, cancels, expiries and refunds to `POST /apple-iap-notifications`. This is the one route with no Cognito authorizer (`PUBLIC_ROUTES` in `scripts/create-api-routes.sh`), and each notification's signature is verified.
5. `eabu-ad-reminder` runs daily at 14:00 UTC. It emails advertisers through SES when the renewal is 5 days away (once per period, skipped if auto-renew is off) and hides paid ads more than a day past `paidThrough`.

Ads may have up to four pictures: `POST /listing-picture` / `DELETE /listing-picture/ad/{zipcode}/{ID}/{slot}` with category `ad` (owner = `ownerUserId`, same Rekognition check), stored at `ads/{ID}/{slot}.jpg` (outside `listings/`, so no 60-day expiry), `picture1..4` on the ad row, and `pictures` links on `GET /eabu-ad/...`, `/guest/ads` and My Ad; deleting the account deletes them (2.0.1). `GET /eabu-ad/active` returns paid ads with a future `paidThrough` in the `?zip`'s country (all of them without `?zip`), flagged `nationwide: true`. To take a paid ad down, set its `status` to `removed`; neither a purchase nor a renewal brings it back. Production purchases are verified with the app's Apple ID (`AppAppleId`, 6826959691). Sandbox purchases (TestFlight, App Review) are accepted while `IapAllowSandbox` is `"true"`. The lookups scan the ad table, which is fine while it holds a few hundred rows.

### Extra posts (Apple in-app consumable)

Sellers get `helpers.MONTHLY_POST_LIMIT` (10) free posts each calendar month in total across all four categories (`helpers.monthly_post_counts` queries every seller table, so each post function may Query all four `userId-index`es); the count resets on the 1st, US Eastern (`helpers.post_limit_cutoff`, `free_posts_reset_on`). Reposting a listing from before this month counts as a post. Past that, each post spends one **extra post**. Extra posts are bought in packs of `helpers.POSTS_PER_PACK` (15) for $0.99, with the consumable `net.eabu.app.posts15` (template parameter `PostsProductId`). They never expire (App Review guideline 3.1.1 forbids expiring paid credits) and work in every category.

- `eabu-post-credits` (table, `scripts/create-post-credits-table.sh`, outside the stack) holds `{userId, balance, purchased}` per seller, plus one row per credited Apple transaction (`userId = "tx#<transactionId>"`), so a replayed purchase is never counted twice.
- `helpers.post_allowance(table, user_id)` returns `(response, spent)`: it lets the post through under the free limit, otherwise spends one extra post atomically, otherwise returns 429 `POST_LIMIT` with `canBuyMore`. If saving the post then fails, the caller calls `helpers.refund_post_credit`.
- `GET /post-credits/{userId}` returns the balance, this month's posts (`usedTotal`, and `used` per category), `freeLimit`, `resetsOn`, `productId` and `appAccountToken`; the app's posting screen works from it. `POST /post-credits` receives the signed transaction, verifies it (`apple_iap.check_posts_purchase`) and adds the pack (function `eabu-post-credits`).
- A `freeLimit` number on a seller's credits row overrides the free limit for that account only (`helpers.free_post_limit`; the app reads it from `GET /post-credits`). Used to test with a low limit; remove the attribute to restore 10.
- A `REFUND`/`REVOKE` notification for the pack (`eabu-apple-notifications`) takes its posts back, never below zero.

### Function naming conventions

Functions follow a `{role}-{action}-{category}` pattern:
- `provider-post-{eat|live|move|lifestyle}` — provider creates a service listing
- `provider-get-{eat|live|move|lifestyle}` — provider retrieves their listings
- `provider-offer-{eat|live|move|lifestyle}` — provider submits an offer in response to a consumer request
- `provider-my-{eat|live|move|lifestyle}-services` — provider views their own active services
- `consumer-post-{eat|live|move|lifestyle}` — consumer posts a service request
- `consumer-get-{eat|live|move|lifestyle}` — consumer browses available services by zipcode
- `consumer-{eat|live|move|lifestyle}-offers` — consumer views offers received

### Security conventions

- **Caller identity**: never trust a `userId`/`cUserId`/`pUserId` from the body, path or query. Compare it to the authenticated caller with `helpers.authorize_user(event, claimed_id)` (or `helpers.require_authenticated(event)` / `helpers.is_owner(...)`). The caller comes from the API Gateway Cognito authorizer claims in `event['requestContext']`. `AUTH_MODE` (`off` = log would-be rejections, `enforce` = reject) and `AUTH_USER_ID_CLAIM` are template parameters.
- **IAM**: every function in `template.yaml` has its own least-privilege `Policies` block listing only the tables/actions/objects it uses. A new function or a new DynamoDB/S3/SES call needs a matching policy entry, or it will get AccessDenied.
- **Identity data**: never log request bodies, images or PII; never return `str(e)`, table names or region in responses. The identity bucket is hardened by `scripts/harden-identity-bucket.sh` (run from CloudShell).
- Security regression tests live in `tests/unit/test_security.py` (moto-based).
- **Anti-scraping**: the API stage `dev` is throttled to 50 requests/s (burst 100), set by hand with `aws apigateway update-stage` (it was the 10,000/s default). The 13 read routes that show other people's data (`consumer-get-*`, `provider-get-*`, `eabu-favourites`, `eabu-discovered`, `eabu-user`, `eabu-get-user-ratings`, `eabu-advertisement-get`) carry `@helpers.rate_limited`: each signed-in user gets `ReadLimitPerHour` (600) reads per UTC hour, counted in `eabu-rate-limit` (`scripts/create-rate-limit-table.sh`, TTL), then 429 `RATE_LIMITED`. It fails open. A new read route showing others' data should get the decorator and the table's `UpdateItem` policy.

### Get functions: query pattern

GET functions query DynamoDB by `zipcode` (partition key) + `ID > 0`, filtered to records created within the last 30 days. When a `userId` path parameter is not `"NA"`, they instead query a `userId-index` GSI to return only that user's items. Use `helpers.query_all(table, ...)` rather than a single `table.query`: one call stops at 1 MB, and `query_all` follows `LastEvaluatedKey`. Create the table resource at module level (once per container), not inside the handler. Numbers go out as strings (`decimal_serializer`), which the app expects. The 16 per-category read functions (`consumer-get-*`, `provider-get-*`, `provider-my-*-services`, `consumer-*-offers`) are identical apart from the category, so keep them in step.

### Invite friends (referral reward)

Sign-up has an optional *Invited by* (a friend's User Id), sent as Cognito `clientMetadata.invitedBy`. `eabu-pre-signup` refuses an unknown, disabled or own User Id with a message, and saves `{userId: "ref#<new user lower case>", invitedBy, emailHash}` in `eabu-post-credits`. After the new user's first post or request (all 8 post functions call `helpers.reward_referral`, best effort, never failing the post), both get `helpers.REFERRAL_POSTS` (5) extra posts; the response carries `referralPosts`. The inviter is paid at most `REFERRAL_MONTHLY_CAP` (10) times a UTC month (`invitesMonth`/`invitesThisMonth`/`invitesTotal` on their credits row); each email earns once ever (`refemail#<hash>` row, kept after account deletion; the `ref#` row is deleted with the account). `invitesTotal >= COMMUNITY_BUILDER_INVITES` (3) is the Community Builder badge, returned by `GET /eabu-user/{id}` (`communityBuilder`); `GET /post-credits/{userId}` returns `invites` progress.

### Share pages (www.eabu.net/p/<token>, /invite)

Share in the app calls `POST /share-link` (`eabu-public-post`, signed in) with `{kind: seller|buyer, category, zipcode, ID}` and shares the returned `https://www.eabu.net/p/<kind>-<category>-<zip>-<ID>-<hmac>` link, so the link preview shows that post rather than the App Store page. The HMAC key is the SSM SecureString `/eabu/share-link-secret` (created by hand, outside the stack); changing it breaks every shared link. `GET /public-post/{token}` is a public route (`PUBLIC_ROUTES`) returning only title, category, short description, ZIP, date and a 10-minute link to the first listing picture, or 404 once the post is gone or expired. The website side is Cloudflare Pages Functions in the app repo: `functions/p/[token].js` (page with Open Graph tags, `noindex`), `functions/p/[token]/image.js` (picture proxy for previews) and `functions/invite.js` (`/invite?from=<User Id>`, preview `invite-card.png`); shared markup in `web/sharePage.js`.

### Discovered near you (weekly web food finds)

`eabu-discover` runs Mondays at 10:00 UTC. It groups the ZIPs of the last 60 days' posts, plus the centres of areas viewed in the last 28 days (`lastViewedAt`), into areas (busiest ZIP is a centre, ZIPs within 25 miles join it; at most `DiscoverMaxAreas`, 40) and invokes itself once per area (async, one retry). Each area run asks Claude (`claude-haiku-5-5`, web search + web fetch, key in SSM SecureString `/eabu/anthropic-api-key`, created by hand) for this week's new or limited food finds, then keeps only those with a specific https article dated within 14 days, medium/high confidence, at most 2 per article, passing `content_filter`, and not shown last week. Up to 12 food places plus 5 for each of the five Lifestyle topics (fitness, yoga, beauty, pets, and `dogparks` since 2026-11-26; all every week since 2026-11-23, rated 3.0+; dog parks need 5 reviews instead of 15, `TOPIC_MIN_REVIEWS`) and up to 4 events are saved in `eabu-discovered` (`areaZip` + `ID`; ID 0 = the area with its member `zips` and `shownKeys`, finds are `yyyymmdd*100+n` for the week's Monday; TTL `expiresAt` 8 days; `scripts/create-discovered-table.sh`). Only ZIP codes go to Claude. About $0.50-0.80 per area per run. A place shown in the last 14 days (`SKIP_DAYS`) isn't shown again. `GET /discovered?zip=` (`eabu-discovered`, signed in) returns the newest week's finds for the caller's area (member ZIP, else nearest centre within 25 miles), cached 10 minutes per container. Any US ZIP gets covered: a known ZIP with no area becomes a `pending` area and `GET /discovered` starts its first search at once (async invoke; cards ~1-2 minutes later; the response has `pending: true`), at most `DISCOVER_NEW_AREAS_PER_DAY` (5, set in `template.yaml`; was 20 until 2026-11-25) a UTC day (counter rows `daily#YYYY-MM-DD`), else it waits for Monday. Viewing an area sets `lastViewedAt` (once a day) and pushes its row's `expiresAt` 28 days out, so unviewed areas drop out. The app mixes them into the buyer Home list like ads. Reports use `POST /report` with `contentType: discovered`, `reportedUserId: eabu-discovered`, `zipcode` = areaZip, `itemId` = ID; remove one with `scripts/moderate.py remove-discovered <areaZip> <ID>`. `scripts/discover_food.py` is the local test version of the prompt (keep the two prompts in step).

### Update prompt (2.3)

`GET /guest/status` also returns `latestVersion` / `minVersion` when SSM `/eabu/app-version` (`{"latest", "min"}`) sets them; `python3 scripts/app_version.py status | latest 2.3 | min 2.1 | latest off | min off` writes it (30 s cache). The app (2.3+, iPhone only, `components/MaintenanceGate.js` + `components/appUpdate.js`) compares its own version: older than `latestVersion` shows a dismissible "A new version of eabu is available" banner with Update (App Store page; "Later" hides it 3 days for that version); older than `minVersion` shows a full "Time to update eabu" screen instead of the app (a pause message wins over it). Set `latest` only after Apple releases the version; use `min` only when the backend stops serving older apps. Apps before 2.3 ignore both. Tests in `tests/unit/test_app_status.py`.

### Search on Home (2.3, no extra cost)

Buyers and guests search "Italian food", "yoga", "dog parks" from a bar on Home (app `screens/Search/Search.js`, `components/search.js`). No web search and no Claude call: `?q=` on the existing routes filters what eabu already has, so no new API routes. `GET /discovered?q=` (and `/guest/discovered`) returns only this week's matching Discovered cards; `GET /favourites-feed/{userId}/seller?nearby=1&q=` and `/guest/feed?q=` return only matching posts, best match first (a search isn't counted as an app open; guests' posts are masked before matching, so hidden phone numbers can't be probed). `my_utils/search.py` turns the text into vocabulary terms (`TERMS`: broad like "food", whole Discovered topics, specific like a cuisine or "barber"; household and transport terms (cleaning, plumber, handyman, movers, rides, tutoring, childcare...) match Live/Move posts only, since Discovered has no such topics; unknown words match literally) and scores cards and posts. Misspellings are corrected to the nearest vocabulary word (`fix_word`: 1 edit, 2 for 8+ letters, only a swapped pair for 4-letter words), never a real word: `my_utils/search_real_words.txt` lists English words near the vocabulary (chair is not hair); rebuild it with `python3 scripts/build_search_words.py` after changing `TERMS`, so "Chienese food" finds Chinese places; `/discovered?q=` returns `query.fixed` and the app shows "Showing results for chinese food". Nothing found: the app opens Apple Maps (US, iPhone) or Google Maps with the phrase near the ZIP, or "Post a request". Each `/discovered?q=` search adds 1 per vocabulary key to `searches#<area>#<Monday>` in `eabu-rate-limit` (`t_<key>`, 35-day TTL; never the typed text or who searched), and the Monday `eabu-discover` run adds "people nearby have been searching for: ..." (specific keys searched 2+ times in 4 weeks) to the prompt within the same counts and budget. Keep the cuisine keys in step with `PHOTO_KEYS["eat"]`. Tests: `tests/unit/test_search.py`.

### Seller contact (phone or email)

Seller posts (`provider-post-*`, fields `phone`/`email`) and offers (`provider-offer-*`, `phoneNumber`/`email`, saved as `offererPhoneNumber`/`offererEmail`) need a phone number, an email, or both: `helpers.seller_contact` returns 400 if both are blank or the email is malformed. A post without a phone stores `phone: ""`; `email` is stored only when given and goes out in the Home feed (`FEED_FIELDS`). The app shows Call/Text for a phone and Email (`mailto:`) for an email. Buyer posts have no email.

### Post expiry (60 days)

Posts in the 8 consumer/provider tables carry `expiresAt` (epoch seconds, `helpers.post_expires_at()`), set on create and refreshed on repost. DynamoDB TTL on `expiresAt` deletes them about 60 days after the last post or repost (`helpers.POST_TTL_DAYS`). Seller pictures under `listings/` in the picture bucket expire 60 days after their last write (S3 lifecycle rule `ExpireListingPicturesAfter60Days`), and `provider-repost` copies them onto themselves (`helpers.refresh_listing_pictures`) to restart their clock. `scripts/enable_post_expiry.py [--dry-run]` turns this on and backfills `expiresAt` on existing posts; it is safe to re-run.
