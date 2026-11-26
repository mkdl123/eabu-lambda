#!/usr/bin/env python3
"""Where eabu is being opened: app opens per day, country, state and ZIP/PIN code.

Counted by the backend (helpers.count_open) each time Home loads its first feed page, signed in
or as a guest. "People" = different users (or guest IP addresses) that day in that ZIP; "opens"
includes refreshes and Buy/Sell switches. Days are UTC. Only postal codes are stored.

    python3 scripts/opens.py              # last 7 days
    python3 scripts/opens.py --days 30
    python3 scripts/opens.py --country IN
"""
import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import boto3
from boto3.dynamodb.conditions import Attr

# Approximate state by the first 3 digits of a US ZIP.
US_RANGES = [
    (5, 5, "NY"), (6, 9, "PR"), (10, 27, "MA"), (28, 29, "RI"), (30, 38, "NH"), (39, 49, "ME"),
    (50, 54, "VT"), (55, 55, "MA"), (56, 59, "VT"), (60, 69, "CT"), (70, 89, "NJ"), (90, 99, "Military"),
    (100, 149, "NY"), (150, 196, "PA"), (197, 199, "DE"), (200, 205, "DC"), (206, 219, "MD"),
    (220, 246, "VA"), (247, 268, "WV"), (270, 289, "NC"), (290, 299, "SC"), (300, 319, "GA"),
    (320, 349, "FL"), (350, 369, "AL"), (370, 385, "TN"), (386, 397, "MS"), (398, 399, "GA"),
    (400, 427, "KY"), (430, 458, "OH"), (460, 479, "IN"), (480, 499, "MI"), (500, 528, "IA"),
    (530, 549, "WI"), (550, 567, "MN"), (569, 569, "DC"), (570, 577, "SD"), (580, 588, "ND"),
    (590, 599, "MT"), (600, 629, "IL"), (630, 658, "MO"), (660, 679, "KS"), (680, 693, "NE"),
    (700, 714, "LA"), (716, 729, "AR"), (730, 732, "OK"), (733, 733, "TX"), (734, 749, "OK"),
    (750, 799, "TX"), (800, 816, "CO"), (820, 831, "WY"), (832, 838, "ID"), (840, 847, "UT"),
    (850, 865, "AZ"), (870, 884, "NM"), (885, 885, "TX"), (889, 898, "NV"), (900, 961, "CA"),
    (967, 968, "HI"), (969, 969, "GU"), (970, 979, "OR"), (980, 994, "WA"), (995, 999, "AK"),
]
# Approximate state/region by the first 2 digits of an India PIN.
IN_RANGES = [
    (11, 11, "Delhi"), (12, 13, "Haryana"), (14, 16, "Punjab/Chandigarh"), (17, 17, "Himachal"),
    (18, 19, "J&K/Ladakh"), (20, 28, "UP/Uttarakhand"), (30, 34, "Rajasthan"), (36, 39, "Gujarat"),
    (40, 44, "Maharashtra/Goa"), (45, 48, "Madhya Pradesh"), (49, 49, "Chhattisgarh"),
    (50, 53, "Telangana/Andhra"), (56, 59, "Karnataka"), (60, 64, "Tamil Nadu"), (67, 69, "Kerala"),
    (70, 74, "West Bengal"), (75, 77, "Odisha"), (78, 78, "Assam"), (79, 79, "North East"),
    (80, 85, "Bihar/Jharkhand"),
]


def region_of(z):
    if len(z) == 5:
        p, ranges = int(z[:3]), US_RANGES
    else:
        p, ranges = int(z[:2]), IN_RANGES
    return next((name for lo, hi, name in ranges if lo <= p <= hi), "?")


def load(table, since):
    kwargs = {"FilterExpression": Attr("key").begins_with("opens#") & Attr("day").gte(since)}
    rows = []
    while True:
        page = table.scan(**kwargs)
        rows += page.get("Items", [])
        if "LastEvaluatedKey" not in page:
            return rows
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def n(row, field):
    return int(row.get(field, 0))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--country", choices=["US", "IN"])
    ap.add_argument("--table", default="eabu-rate-limit")
    ap.add_argument("--region", default="us-east-1")
    a = ap.parse_args()

    since = (datetime.now(timezone.utc) - timedelta(days=a.days - 1)).strftime("%Y-%m-%d")
    table = boto3.resource("dynamodb", region_name=a.region).Table(a.table)
    rows = [r for r in load(table, since) if not a.country or r.get("country") == a.country]
    if not rows:
        print(f"No opens since {since} (UTC).")
        return

    print(f"App opens since {since} (UTC days). people = different users/guests that day; "
          "opens include refreshes.\n")
    print(f"{'Day':<12}{'Country':<9}{'People':>8}{'(guests)':>10}{'Opens':>8}")
    days = defaultdict(lambda: defaultdict(int))
    for r in rows:
        d = days[(r["day"], r.get("country") or "?")]
        d["people"] += n(r, "userPeople") + n(r, "guestPeople")
        d["guests"] += n(r, "guestPeople")
        d["opens"] += n(r, "userOpens") + n(r, "guestOpens")
    for (day, country), d in sorted(days.items(), reverse=True):
        print(f"{day:<12}{country:<9}{d['people']:>8}{d['guests']:>10}{d['opens']:>8}")

    print(f"\n{'ZIP/PIN':<9}{'Area (approx)':<20}{'Days':>6}{'People':>8}{'(guests)':>10}{'Opens':>8}  Last seen")
    zips = defaultdict(lambda: {"days": set(), "people": 0, "guests": 0, "opens": 0})
    for r in rows:
        z = zips[r["zip"]]
        z["days"].add(r["day"])
        z["people"] += n(r, "userPeople") + n(r, "guestPeople")
        z["guests"] += n(r, "guestPeople")
        z["opens"] += n(r, "userOpens") + n(r, "guestOpens")
    for zip_code, z in sorted(zips.items(), key=lambda kv: (-kv[1]["people"], -kv[1]["opens"], kv[0])):
        area = f"{'US' if len(zip_code) == 5 else 'IN'} {region_of(zip_code)}"
        print(f"{zip_code:<9}{area:<20}{len(z['days']):>6}{z['people']:>8}{z['guests']:>10}{z['opens']:>8}"
              f"  {max(z['days'])}")
    print("\nPeople add up per day, so someone who opens on 3 days counts 3 times above.")


if __name__ == "__main__":
    main()
