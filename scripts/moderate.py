#!/usr/bin/env python3
"""Act on content reports (App Store guideline 1.2: within 24 hours).

Reports arrive by email (to ReportEmail) and are kept in eabu-feedback. Every command that
changes something only shows what it would do until you add --yes.

    python3 scripts/moderate.py reports                 # open reports, newest first
    python3 scripts/moderate.py reports --all           # include resolved ones
    python3 scripts/moderate.py user <userId>           # their posts, offers, ads, reports, account
    python3 scripts/moderate.py remove-post <category> <listing|request> <zipcode> <ID> --yes
                                                        # delete one post (and its pictures)
    python3 scripts/moderate.py ban <userId> --yes      # disable the account, sign it out everywhere,
                                                        # delete its posts and offers, take its ads down
    python3 scripts/moderate.py unban <userId> --yes    # let the account sign in again (posts stay deleted)
    python3 scripts/moderate.py resolve <reportID> [--note "removed post"] --yes

category is eat, live, move or lifestyle. A listing is a seller's post, a request a buyer's.
The report email shows the category, ZIP and ID. To delete an account and all its data
for good, use scripts/delete_user.py instead.

Runs with your local AWS credentials (needs a Python with boto3).
"""
import argparse
import sys

import boto3
from boto3.dynamodb.conditions import Attr, Key

REGION = "us-east-1"
POOL_ID = "us-east-1_G0VobzUMd"
PICTURE_BUCKET = "eabu-identity-pictures"
FEEDBACK_TABLE = "eabu-feedback"
AD_TABLE = "eabu-advertisement"
CATEGORIES = ("eat", "live", "move", "lifestyle")
ROLES = {"listing": "provider", "request": "consumer"}
PICTURE_SLOTS = ("1", "2", "3", "4")
COUNTER_ID = 100  # the sentinel counter row in every post table

ddb = boto3.resource("dynamodb", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)
cognito = boto3.client("cognito-idp", region_name=REGION)


def post_table(category, kind):
    return ddb.Table(f"eabu-{category}-{ROLES[kind]}")


def offer_table(category):
    return ddb.Table(f"eabu-{category}-offer")


def _all(method, **kwargs):
    items = []
    while True:
        page = method(**kwargs)
        items += page.get("Items", [])
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _num(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _short(text, n=70):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


# --- reading -----------------------------------------------------------------------------

def list_reports(include_resolved=False):
    rows = _all(ddb.Table(FEEDBACK_TABLE).scan, FilterExpression=Attr("type").eq("report"))
    if not include_resolved:
        rows = [r for r in rows if r.get("status", "open") == "open"]
    return sorted(rows, key=lambda r: r.get("createdAt", ""), reverse=True)


def posts_of(user_id):
    out = []
    for category in CATEGORIES:
        for kind in ROLES:
            for item in _all(post_table(category, kind).query, IndexName="userId-index",
                             KeyConditionExpression=Key("userId").eq(user_id)):
                if _num(item.get("ID")) != COUNTER_ID:
                    out.append({**item, "_category": category, "_kind": kind})
    return out


def offers_by(user_id):
    """Offers this user made as a seller (pUserId; the offer tables are small, so a scan is fine)."""
    out = []
    for category in CATEGORIES:
        for item in _all(offer_table(category).scan, FilterExpression=Attr("pUserId").eq(user_id)):
            out.append({**item, "_category": category})
    return out


def ads_of(user_id):
    return _all(ddb.Table(AD_TABLE).scan, FilterExpression=Attr("ownerUserId").eq(user_id))


def account_status(user_id):
    try:
        user = cognito.admin_get_user(UserPoolId=POOL_ID, Username=user_id)
    except cognito.exceptions.UserNotFoundException:
        return "no account"
    return "enabled" if user.get("Enabled", True) else "DISABLED (banned)"


# --- changing ----------------------------------------------------------------------------

def delete_post(category, kind, item):
    post_table(category, kind).delete_item(Key={"zipcode": item["zipcode"], "ID": item["ID"]})
    if kind == "listing":
        for slot in PICTURE_SLOTS:
            if item.get(f"picture{slot}"):
                s3.delete_object(Bucket=PICTURE_BUCKET, Key=f"listings/{category}/{int(item['ID'])}/{slot}.jpg")


def ban(user_id):
    """Disable the account, end its sessions, delete its posts and offers, take its ads down."""
    counts = {"posts": 0, "offers": 0, "ads": 0}
    cognito.admin_disable_user(UserPoolId=POOL_ID, Username=user_id)
    cognito.admin_user_global_sign_out(UserPoolId=POOL_ID, Username=user_id)
    for post in posts_of(user_id):
        delete_post(post["_category"], post["_kind"], post)
        counts["posts"] += 1
    for offer in offers_by(user_id):
        offer_table(offer["_category"]).delete_item(Key={"zipcode": offer["zipcode"], "ID": offer["ID"]})
        counts["offers"] += 1
    for ad in ads_of(user_id):
        if ad.get("status") != "removed":
            ddb.Table(AD_TABLE).update_item(Key={"zipcode": ad["zipcode"], "ID": ad["ID"]},
                                            UpdateExpression="SET #s = :r",
                                            ExpressionAttributeNames={"#s": "status"},
                                            ExpressionAttributeValues={":r": "removed"})
            counts["ads"] += 1
    return counts


def unban(user_id):
    cognito.admin_enable_user(UserPoolId=POOL_ID, Username=user_id)


def resolve(report_id, note=""):
    matches = [r for r in list_reports(include_resolved=True) if str(r["ID"]) == str(report_id)]
    if not matches:
        return None
    row = matches[0]
    names, values = {"#s": "status"}, {":s": "resolved"}
    expr = "SET #s = :s"
    if note:
        expr += ", resolution = :n"
        values[":n"] = note[:500]
    ddb.Table(FEEDBACK_TABLE).update_item(Key={"createdAt": row["createdAt"], "ID": row["ID"]},
                                          UpdateExpression=expr, ExpressionAttributeNames=names,
                                          ExpressionAttributeValues=values)
    return row


# --- command line ------------------------------------------------------------------------

def _print_report(r):
    where = " / ".join(str(r[k]) for k in ("category", "zipcode", "itemId") if r.get(k))
    print(f"#{r['ID']}  {str(r.get('createdAt', ''))[:16]}  [{r.get('status', 'open')}]  "
          f"{r.get('reason')} {r.get('contentType')}")
    print(f"    reported: {r.get('reportedUserId')}   by: {r.get('reporterUserId')}   where: {where or '-'}")
    if r.get("title"):
        print(f"    title: {_short(r['title'])}")
    if r.get("details"):
        print(f"    details: {_short(r['details'], 200)}")
    if r.get("resolution"):
        print(f"    resolution: {r['resolution']}")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("reports")
    r.add_argument("--all", action="store_true")
    u = sub.add_parser("user")
    u.add_argument("user_id")
    rp = sub.add_parser("remove-post")
    rp.add_argument("category", choices=CATEGORIES)
    rp.add_argument("kind", choices=tuple(ROLES))
    rp.add_argument("zipcode")
    rp.add_argument("item_id", type=int)
    rp.add_argument("--yes", action="store_true")
    for name in ("ban", "unban"):
        b = sub.add_parser(name)
        b.add_argument("user_id")
        b.add_argument("--yes", action="store_true")
    rs = sub.add_parser("resolve")
    rs.add_argument("report_id")
    rs.add_argument("--note", default="")
    rs.add_argument("--yes", action="store_true")
    args = p.parse_args(argv)

    if args.cmd == "reports":
        rows = list_reports(args.all)
        print(f"{len(rows)} {'' if args.all else 'open '}report(s)\n")
        for row in rows:
            _print_report(row)
            print()
        return 0

    if args.cmd == "user":
        uid = args.user_id
        print(f"Account {uid}: {account_status(uid)}\n")
        posts = posts_of(uid)
        print(f"Posts ({len(posts)}):")
        for x in posts:
            print(f"  {x['_category']} {x['_kind']} {x['zipcode']} {x['ID']}  {_short(x.get('title'))}")
        offers = offers_by(uid)
        print(f"\nOffers made ({len(offers)}):")
        for x in offers:
            print(f"  {x['_category']} {x['zipcode']} {x['ID']} on request {x.get('itemId')}  {_short(x.get('offerDescription'))}")
        ads = ads_of(uid)
        print(f"\nAds ({len(ads)}):")
        for x in ads:
            print(f"  {x['zipcode']} {x['ID']} [{x.get('status')}]  {_short(x.get('title'))}")
        against = [x for x in list_reports(True) if x.get("reportedUserId") == uid]
        print(f"\nReports about them ({len(against)}):")
        for x in against:
            _print_report(x)
        return 0

    if args.cmd == "remove-post":
        table = post_table(args.category, args.kind)
        item = table.get_item(Key={"zipcode": args.zipcode, "ID": args.item_id}).get("Item")
        if not item or args.item_id == COUNTER_ID:
            print("No such post (check category, listing/request, ZIP and ID).")
            return 1
        print(f"{args.category} {args.kind} {args.zipcode} {args.item_id} by {item.get('userId')}: {_short(item.get('title'))}")
        if not args.yes:
            print("Not deleted. Add --yes to delete it.")
            return 0
        delete_post(args.category, args.kind, item)
        print("Deleted.")
        return 0

    if args.cmd == "ban":
        uid = args.user_id
        status = account_status(uid)
        if status == "no account":
            print(f"{uid}: no such account.")
            return 1
        print(f"{uid}: {status}; {len(posts_of(uid))} post(s), {len(offers_by(uid))} offer(s), "
              f"{len([a for a in ads_of(uid) if a.get('status') != 'removed'])} ad(s) to take down.")
        if not args.yes:
            print("Nothing changed. Add --yes to ban.")
            return 0
        counts = ban(uid)
        print(f"Banned {uid}: account disabled and signed out; deleted {counts['posts']} post(s) "
              f"and {counts['offers']} offer(s); took down {counts['ads']} ad(s).")
        print("A paid ad's Apple subscription keeps renewing until the advertiser cancels it or Apple refunds it.")
        return 0

    if args.cmd == "unban":
        uid = args.user_id
        print(f"{uid}: {account_status(uid)}")
        if not args.yes:
            print("Nothing changed. Add --yes to let them sign in again.")
            return 0
        unban(uid)
        print(f"{uid} can sign in again. Their deleted posts stay deleted.")
        return 0

    if args.cmd == "resolve":
        if not args.yes:
            print(f"Would mark report #{args.report_id} resolved. Add --yes.")
            return 0
        row = resolve(args.report_id, args.note)
        if not row:
            print(f"No report #{args.report_id}.")
            return 1
        print(f"Report #{args.report_id} marked resolved.")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
