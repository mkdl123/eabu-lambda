#!/usr/bin/env python3
"""Seller recruitment: find local businesses near a ZIP, review the list, email them an invite.

Reads Overture Maps Places (free, open data: CDLA Permissive 2.0 / ODbL), only the area around
the ZIP, and writes a CSV you review before anything is sent. Keeps businesses that are open (or
unknown but high-confidence), not chains, within --miles. Emails come from Overture; with
--crawl, a missing email is looked up on the business's own website (home and contact page,
robots.txt respected), which also drops businesses whose website is dead.

    python3 scripts/recruit.py categories
    python3 scripts/recruit.py find --zip 19060 --category cleaning
    python3 scripts/recruit.py find --zip 19060 --category all --miles 10 --crawl

Output: recruit/<zip>-<category>-<date>.csv (gitignored: other businesses' contact details).
Review it and delete the rows you don't want, then send:

    python3 scripts/recruit.py address "PO Box 1, Town, ST 12345"   # once: footer address (law)
    python3 scripts/recruit.py send recruit/19060-cleaning-2026-11-27.csv --to admin@eabu.net
    python3 scripts/recruit.py send recruit/19060-cleaning-2026-11-27.csv          # who would get it
    python3 scripts/recruit.py send recruit/19060-cleaning-2026-11-27.csv --yes    # send (max 50/day)
    python3 scripts/recruit.py status

A spreadsheet of your own works the same way: only column A (the emails) is read, and the file
name picks the email. ad*.xlsx sends docs/marketing-email-business.html, seller*.xlsx sends
docs/marketing-email-sellers.html (the subject is the page's <title>):

    python3 scripts/recruit.py send ad.xlsx --to admin@eabu.net    # one preview
    python3 scripts/recruit.py send seller.xlsx                    # who would get it
    python3 scripts/recruit.py send seller.xlsx --yes              # send (same daily cap and dedupe)

Run a sheet again any time: an address it already emailed gets the email again only once 30 days
(RESEND_DAYS) have passed since its last email; unsubscribed addresses never. CSV invites go once ever.

The invite is scripts/recruit_email/invite.{txt,html}, from "Muk at eabu <hello@invite.eabu.net>",
replies to admin@eabu.net. Every business is emailed at most once ever: eabu-recruit keeps the
sha256 of each email (never the email), and the footer's unsubscribe link
(GET/POST /guest/unsubscribe, eabu-recruit-unsubscribe) marks that row. SES's account
suppression list also stops addresses that bounced or complained. --to sends one preview of the
first row to your own address and records nothing.
"""
import argparse
import csv
import hashlib
import html
import math
import os
import re
import sys
import time
import urllib.robotparser
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr
from urllib.parse import urljoin, urlparse

import boto3
import requests
from botocore import UNSIGNED
from botocore.config import Config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "recruit")
CACHE_DIR = os.path.join(OUT_DIR, "cache")
CENTROIDS = os.path.join(ROOT, "eabu-dish", "python", "my_utils", "zip_centroids.csv")
BUCKET = "overturemaps-us-west-2"
USER_AGENT = "eabu-recruit/1.0 (+https://www.eabu.net)"
MIN_CONFIDENCE_UNKNOWN_STATUS = 0.8

REGION = "us-east-1"
RECRUIT_TABLE = "eabu-recruit"
ADDRESS_PARAM = "/eabu/postal-address"
TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recruit_email")
FROM = formataddr(("Muk at eabu", "hello@invite.eabu.net"))
REPLY_TO = "admin@eabu.net"
UNSUBSCRIBE_URL = "https://api.eabu.net/guest/unsubscribe?t={token}"
DOCS_DIR = os.path.join(ROOT, "docs")
# spreadsheet name (ad.xlsx, seller.xlsx, ad-miami.xlsx ...) -> email page sent to column A
XLSX_TEMPLATES = {"ad": "marketing-email-business.html", "seller": "marketing-email-sellers.html",
                  "sellers": "marketing-email-sellers.html"}
CAP_TIMEZONE = "America/New_York"  # the daily cap resets at midnight US Eastern
RESEND_DAYS = 30        # marketing sheets: an address gets at most one email per 30 days
DAILY_CAP = 50          # new sending address: build trust with mail providers slowly
SEND_GAP_SECONDS = 2

# eabu category -> Overture taxonomy.primary values. Small local services that could post on eabu.
CATEGORIES = {
    "cleaning": ["home_cleaning", "cleaning_service", "janitorial_service", "carpet_cleaning",
                 "office_cleaning", "pool_cleaning", "laundry_service"],
    "catering": ["caterer", "food_truck_stand", "personal_chef", "food_delivery_service"],
    "home": ["handyman", "painting", "plumbing", "electrician", "landscaping", "lawn_service",
             "pest_control_service", "sewing_and_alterations"],
    "moving": ["mover", "junk_removal_and_hauling", "limo_service", "taxi_service"],
    "fitness": ["fitness_trainer", "yoga_studio", "pilates_studio", "boot_camp", "boxing_class",
                "dance_studio"],
    "beauty": ["hair_salon", "beauty_salon", "nail_salon", "barber", "makeup_artist",
               "skin_care_and_makeup", "waxing", "massage_therapy", "hair_stylist", "eyelash_service"],
    "pets": ["pet_groomer", "dog_trainer", "pet_sitting", "dog_walker", "pet_boarding"],
    "tutoring": ["tutoring_service"],
    "childcare": ["child_care_and_day_care", "babysitter", "nanny_service"],
}
EABU_CATEGORY = {"cleaning": "Live", "home": "Live", "childcare": "Live", "tutoring": "Live",
                 "catering": "Eat", "moving": "Move",
                 "fitness": "Lifestyle", "beauty": "Lifestyle", "pets": "Lifestyle"}

# How the invite names what they do: "one of the first {service} businesses".
SERVICE_WORD = {"cleaning": "cleaning", "catering": "catering", "home": "home service",
                "moving": "moving and rides", "fitness": "fitness", "beauty": "beauty",
                "pets": "pet care", "tutoring": "tutoring", "childcare": "childcare"}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
BAD_EMAIL = re.compile(r"(example\.|sentry|wixpress|\.png$|\.jpe?g$|\.gif$|\.webp$|\.svg$|domain\.com|"
                       r"yourdomain|email\.com$|godaddy|squarespace|@facebook\.com|@instagram\.com|noreply|no-reply|donotreply)", re.I)


def zip_centroid(zipcode):
    with open(CENTROIDS) as f:
        for line in f:
            if line.startswith(zipcode + ","):
                _, lat, lng = line.strip().split(",")
                return float(lat), float(lng)
    sys.exit(f"unknown US ZIP {zipcode}")


def miles(lat1, lng1, lat2, lng2):
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lng2 - lng1) * p / 2) ** 2)
    return 3958.8 * 2 * math.asin(math.sqrt(a))


def latest_release():
    s3 = boto3.client("s3", region_name="us-west-2", config=Config(signature_version=UNSIGNED))
    out = s3.list_objects_v2(Bucket=BUCKET, Prefix="release/", Delimiter="/")
    return sorted(p["Prefix"].split("/")[1] for p in out.get("CommonPrefixes", []))[-1]


def area_places(zipcode, lat, lng, radius):
    """Every place in the box around the ZIP, cached per ZIP/radius/release."""
    import duckdb  # only find needs it (pip install duckdb)

    release = latest_release()
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"{zipcode}-{radius}mi-{release}.parquet")
    if not os.path.exists(path):
        dlat = radius / 69.0
        dlng = radius / (69.0 * math.cos(lat * math.pi / 180))
        print(f"reading Overture {release} around {zipcode} ...", file=sys.stderr)
        c = duckdb.connect()
        c.sql("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';")
        c.sql(f"""COPY (SELECT id, names.primary AS name, taxonomy.primary AS tax, operating_status,
                confidence, phones, websites, emails, addresses[1].freeform AS street,
                addresses[1].locality AS city, addresses[1].region AS region,
                addresses[1].postcode AS zip, bbox.ymin AS lat, bbox.xmin AS lng,
                brand.names.primary AS brand
            FROM read_parquet('s3://{BUCKET}/release/{release}/theme=places/type=place/*', hive_partitioning=1)
            WHERE bbox.xmin BETWEEN {lng - dlng} AND {lng + dlng}
              AND bbox.ymin BETWEEN {lat - dlat} AND {lat + dlat}) TO '{path}'""")
    return duckdb.connect().sql(f"SELECT * FROM '{path}'").fetchall(), release


def clean_email(e):
    e = (e or "").strip().strip(".").lower()
    if e.startswith("mailto:"):
        e = e[7:]
    e = e.split("?")[0]
    return e if EMAIL_RE.fullmatch(e) and not BAD_EMAIL.search(e) else ""


def site_domain(url):
    host = urlparse(url if "://" in url else "http://" + url).hostname or ""
    return host[4:] if host.startswith("www.") else host


def crawl_email(website):
    """(email, site_ok) from the business's own home page and contact page."""
    url = website if "://" in website else "http://" + website
    try:
        rp = urllib.robotparser.RobotFileParser(urljoin(url, "/robots.txt"))
        rp.read()
    except Exception:
        rp = None
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    found, site_ok = [], False
    for path in ["", "/contact", "/contact-us"]:
        page = urljoin(url, path) if path else url
        if rp is not None and rp.mtime() and not rp.can_fetch(USER_AGENT, page):
            continue
        try:
            r = session.get(page, timeout=8, allow_redirects=True)
        except requests.RequestException:
            continue
        if r.status_code >= 400 or "html" not in r.headers.get("content-type", ""):
            continue
        site_ok = True
        found += [e for e in map(clean_email, EMAIL_RE.findall(r.text)) if e]
        if found:
            break
    domain = site_domain(website)
    same = [e for e in found if domain and e.endswith("@" + domain)]
    return (same or found or [""])[0], site_ok


def find(args):
    zipcode = args.zip.strip()
    lat, lng = zip_centroid(zipcode)
    cats = list(CATEGORIES) if args.category == "all" else [args.category]
    tax_to_cat = {t: c for c in cats for t in CATEGORIES[c]}
    rows, release = area_places(zipcode, lat, lng, args.miles)

    seen, out = set(), []
    for (pid, name, tax, status, conf, phones, websites, emails, street, city, region, pzip,
         plat, plng, brand) in rows:
        if tax not in tax_to_cat or brand:
            continue
        if status == "permanently_closed" or status == "temporarily_closed":
            continue
        if status != "open" and (conf or 0) < MIN_CONFIDENCE_UNKNOWN_STATUS:
            continue
        dist = miles(lat, lng, plat, plng)
        if dist > args.miles:
            continue
        email = next((e for e in map(clean_email, emails or []) if e), "")
        phone = (phones or [""])[0]
        website = (websites or [""])[0]
        key = email or phone or (name or "").lower()
        if not name or key in seen:
            continue
        seen.add(key)
        out.append({"name": name, "category": tax_to_cat[tax], "eabu": EABU_CATEGORY[tax_to_cat[tax]],
                    "type": tax, "email": email, "email_from": "overture" if email else "",
                    "phone": phone, "website": website, "street": street or "", "city": city or "",
                    "state": region or "", "zip": (pzip or "")[:5], "miles": round(dist, 1),
                    "status": status or "unknown", "confidence": round(conf or 0, 2),
                    "site_ok": "", "overture_id": pid})

    if args.crawl:
        todo = [r for r in out if r["website"]]
        print(f"checking {len(todo)} websites ...", file=sys.stderr)
        with ThreadPoolExecutor(max_workers=8) as pool:
            for r, (email, ok) in zip(todo, pool.map(lambda r: crawl_email(r["website"]), todo)):
                r["site_ok"] = "yes" if ok else "no"
                if not r["email"] and email:
                    r["email"], r["email_from"] = email, "website"
        out = [r for r in out if r["site_ok"] != "no"]

    out.sort(key=lambda r: (r["email"] == "", r["category"], r["miles"]))
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"{zipcode}-{args.category}-{date.today()}.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()) if out else ["name"])
        w.writeheader()
        w.writerows(out)

    with_email = sum(1 for r in out if r["email"])
    print(f"Overture release {release}; {len(out)} businesses within {args.miles} mi of {zipcode}, "
          f"{with_email} with an email.")
    by_cat = {}
    for r in out:
        n, e = by_cat.get(r["category"], (0, 0))
        by_cat[r["category"]] = (n + 1, e + bool(r["email"]))
    for c, (n, e) in sorted(by_cat.items()):
        print(f"  {c:<10} {n:>4} businesses, {e:>4} with email")
    print(f"saved {path}")


def email_hash(email):
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


def postal_address():
    try:
        return boto3.client("ssm", region_name=REGION).get_parameter(Name=ADDRESS_PARAM)["Parameter"]["Value"]
    except Exception:
        return ""


def render(row, address):
    """(subject, text, html) for one CSV row."""
    name = " ".join((row.get("name") or "").split())[:80]
    city = " ".join((row.get("city") or "").split()).title()
    city = re.sub(r"\bTwp\.?$", "Township", city)
    values = {"name": name, "city": city or "you",
              "service": SERVICE_WORD.get(row.get("category"), "local"),
              "unsubscribe_url": UNSUBSCRIBE_URL.format(token=email_hash(row["email"])),
              "postal_address": f", {address}" if address else ""}
    values["subject"] = f"Customers near {city} for {name}" if city else f"Local customers for {name}"
    text = open(os.path.join(TEMPLATE_DIR, "invite.txt")).read()
    page = open(os.path.join(TEMPLATE_DIR, "invite.html")).read()
    if not city:  # "People around you have started using it"
        text = text.replace("People around {city}", "People around you")
        page = page.replace("People around {city}", "People around you")
    escaped = {k: html.escape(v, quote=True) for k, v in values.items()}
    return values["subject"], text.format(**values), page.format(**escaped)


def build_message(to, subject, text, page, unsubscribe_url):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Reply-To"], msg["Subject"] = FROM, to, REPLY_TO, subject
    msg["List-Unsubscribe"] = f"<{unsubscribe_url}>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg.set_content(text)
    msg.add_alternative(page, subtype="html")
    return msg


XLSX_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
           "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
           "rel": "http://schemas.openxmlformats.org/package/2006/relationships"}


def xlsx_column_a(path):
    """Text of every column A cell on the first sheet (no openpyxl needed)."""
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", XLSX_NS):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{XLSX_NS['m']}}}t")))
        book = ET.fromstring(z.read("xl/workbook.xml"))
        rid = book.find("m:sheets/m:sheet", XLSX_NS).get(f"{{{XLSX_NS['r']}}}id")
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        target = next(r.get("Target") for r in rels.findall("rel:Relationship", XLSX_NS) if r.get("Id") == rid)
        target = target.lstrip("/") if target.startswith("/") else "xl/" + target
        sheet = ET.fromstring(z.read(target))
    out = []
    for c in sheet.iter(f"{{{XLSX_NS['m']}}}c"):
        if not re.fullmatch(r"A\d+", c.get("r", "")):
            continue
        t, v = c.get("t"), c.find("m:v", XLSX_NS)
        if t == "s" and v is not None:
            out.append(shared[int(v.text)])
        elif t == "inlineStr":
            out.append("".join(x.text or "" for x in c.iter(f"{{{XLSX_NS['m']}}}t")))
        elif v is not None:
            out.append(v.text or "")
    return out


def xlsx_template(path):
    """(kind, template path) from the spreadsheet's name, e.g. ad.xlsx -> ad, business email."""
    stem = os.path.splitext(os.path.basename(path))[0].lower()
    kind = re.split(r"[-_ .]", stem)[0]
    if kind not in XLSX_TEMPLATES:
        sys.exit(f"don't know which email to send for {os.path.basename(path)}: name it ad.xlsx "
                 f"(business ad email) or seller.xlsx (sellers email)")
    name = XLSX_TEMPLATES[kind]
    for folder in (os.path.dirname(os.path.abspath(path)), DOCS_DIR):
        if os.path.exists(os.path.join(folder, name)):
            return kind, os.path.join(folder, name)
    sys.exit(f"can't find {name} next to {path} or in docs/")


def html_to_text(page):
    page = re.sub(r"(?is)<(head|style|script)\b.*?</\1>", "", page)
    page = re.sub(r'(?is)<div style="display:none.*?</div>', "", page)  # inbox preview text
    page = re.sub(r"(?is)<!--.*?-->", "", page)
    page = re.sub(r'(?is)<a [^>]*href="(https?://[^"]+)"[^>]*>\s*<img [^>]*alt="([^"]*)"[^>]*>\s*</a>',
                  r"\2: \1", page)
    page = re.sub(r"(?i)<br\s*/?>|</(p|h1|h2|tr|div)>", "\n", page)
    text = html.unescape(re.sub(r"<[^>]+>", "", page))
    lines = [" ".join(l.split()) for l in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip() + "\n"


def render_page(template, unsubscribe_url):
    """(subject, text, html) for a docs/marketing-email-*.html page."""
    page = open(template, encoding="utf-8").read()
    # the in-browser editor (bar + script) never goes out
    page = re.sub(r'(?is)<!-- EDITOR.*?-->\s*', "", page)
    page = re.sub(r'(?is)<div id="eabu-editor-bar".*?</div>\s*', "", page)
    page = re.sub(r'(?is)<script id="eabu-editor-script">.*?</script>\s*', "", page)
    if "mailto:admin@eabu.net?subject=Unsubscribe" not in page or "eabu LLC," not in page:
        sys.exit(f"{os.path.basename(template)}: footer needs the Unsubscribe link and the eabu LLC address")
    page = page.replace("mailto:admin@eabu.net?subject=Unsubscribe", html.escape(unsubscribe_url, quote=True))
    m = re.search(r"(?is)<title>(.*?)</title>", page)
    subject = html.unescape(" ".join(m.group(1).split())) if m else "eabu"
    text = html_to_text(page).replace("Unsubscribe ·", f"Unsubscribe: {unsubscribe_url}\n")
    return subject, text, page


def read_rows(path):
    if path.lower().endswith(".xlsx"):
        kind, _ = xlsx_template(path)
        rows, seen = [], set()
        for cell in xlsx_column_a(path):  # a header such as "Email" isn't an email, so it drops out
            e = clean_email(cell)
            if e and e not in seen:
                seen.add(e)
                rows.append({"name": e, "email": e, "category": kind, "city": "", "zip": ""})
        return rows
    rows, seen = [], set()
    # utf-8-sig drops the BOM Excel adds; Numbers/Excel may also put a title row above the header,
    # so the header is the first row with an "email" column, not necessarily line 1
    with open(path, newline="", encoding="utf-8-sig") as f:
        lines = list(csv.reader(f))
    start = next((i for i, row in enumerate(lines)
                  if "email" in [c.strip().lower() for c in row]), None)
    if start is None:
        return rows
    header = [c.strip().lower() for c in lines[start]]
    for values in lines[start + 1:]:
        r = dict(zip(header, values))
        e = clean_email(r.get("email"))
        if e and e not in seen:
            seen.add(e)
            z = (r.get("zip") or "").strip()
            if z.isdigit() and len(z) < 5:  # a spreadsheet turned 08830 into 8830
                r["zip"] = z.zfill(5)
            rows.append({**r, "email": e})
    return rows


def already_contacted(table, hashes):
    """{emailHash: row} for the addresses already in eabu-recruit."""
    found = {}
    unique = list(dict.fromkeys(hashes))
    for i in range(0, len(unique), 100):
        keys = [{"emailHash": h} for h in unique[i:i + 100]]
        req = {RECRUIT_TABLE: {"Keys": keys, "ProjectionExpression": "emailHash, #s, sentAt",
                               "ExpressionAttributeNames": {"#s": "status"}}}
        while req:
            out = table.meta.client.batch_get_item(RequestItems=req)
            found.update({item["emailHash"]: item for item in out["Responses"].get(RECRUIT_TABLE, [])})
            req = out.get("UnprocessedKeys") or None
    return found


def due_again(item, now=None):
    """A marketing-sheet address may get another email RESEND_DAYS after its last one (never if unsubscribed)."""
    if item.get("status") != "sent" or not item.get("sentAt"):
        return False
    return int(item["sentAt"]) <= (now or time.time()) - RESEND_DAYS * 86400


def send_day(now=None):
    """The day the daily cap counts against: US Eastern, so it resets at midnight there."""
    from zoneinfo import ZoneInfo
    return (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(CAP_TIMEZONE)).strftime("%Y-%m-%d")


def send(args):
    template = xlsx_template(args.csv)[1] if args.csv.lower().endswith(".xlsx") else None
    if template:
        print(f"sending {os.path.relpath(template, ROOT)} to column A of {os.path.basename(args.csv)}")

    def make(row, unsubscribe_url):
        if template:
            return render_page(template, unsubscribe_url)
        return render(row, address)

    address = postal_address()
    if not address and not args.to and not template:  # previews may go out without it; real invites never
        sys.exit('No postal address yet (required by US law in every marketing email). Set it once:\n'
                 '  python3 scripts/recruit.py address "PO Box 1, Town, ST 12345"')
    rows = read_rows(args.csv)
    ses = boto3.client("sesv2", region_name=REGION)

    if args.to:
        if not rows:
            sys.exit("no rows with an email in that file")
        preview_url = UNSUBSCRIBE_URL.format(token="0" * 64)
        subject, text, page = make(rows[0], preview_url)
        msg = build_message(args.to, "[PREVIEW] " + subject, text, page, preview_url)
        ses.send_email(Content={"Raw": {"Data": msg.as_bytes()}})
        print(f"preview for {rows[0]['name']} sent to {args.to} (nothing recorded)")
        return

    table = boto3.resource("dynamodb", region_name=REGION).Table(RECRUIT_TABLE)
    contacted = already_contacted(table, [email_hash(r["email"]) for r in rows])
    # invites (CSV) go once ever; marketing sheets may repeat after RESEND_DAYS
    todo = [r for r in rows if email_hash(r["email"]) not in contacted
            or (template and due_again(contacted[email_hash(r["email"])]))]
    repeats = sum(1 for r in todo if email_hash(r["email"]) in contacted)
    day_key = "daily#" + send_day()
    sent_today = int(table.get_item(Key={"emailHash": day_key}).get("Item", {}).get("count", 0))
    room = max(0, min(args.limit, DAILY_CAP - sent_today))
    held = f"emailed in the last {RESEND_DAYS} days or unsubscribed" if template else "already contacted or unsubscribed"
    print(f"{len(rows)} with an email, {len(rows) - len(todo)} {held}, "
          f"{len(todo) - repeats} new, {repeats} due again. Sent today: {sent_today}/{DAILY_CAP}, so this run sends {min(room, len(todo))}.")
    if not args.yes:
        for r in todo[:room]:
            print(f"  {r['name'][:40]:<40} {r['category']:<9} {r.get('city', '')[:18]:<18} {r['email']}")
        print("nothing sent; add --yes to send these")
        return

    sent = 0
    for r in todo[:room]:
        h = email_hash(r["email"])
        again = h in contacted
        try:
            if again:  # claim it only if still sent and still 30+ days old (not unsubscribed meanwhile)
                table.update_item(Key={"emailHash": h}, UpdateExpression="SET #s = :sending",
                                  ConditionExpression="#s = :sent AND sentAt <= :cutoff",
                                  ExpressionAttributeNames={"#s": "status"},
                                  ExpressionAttributeValues={":sending": "sending", ":sent": "sent",
                                                             ":cutoff": int(time.time()) - RESEND_DAYS * 86400})
            else:
                table.put_item(Item={"emailHash": h, "status": "sending", "category": r.get("category", ""),
                                     "zip": r.get("zip", ""), "createdAt": int(time.time())},
                               ConditionExpression="attribute_not_exists(emailHash)")
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            continue
        subject, text, page = make(r, UNSUBSCRIBE_URL.format(token=h))
        try:
            ses.send_email(Content={"Raw": {"Data": build_message(
                r["email"], subject, text, page, UNSUBSCRIBE_URL.format(token=h)).as_bytes()}})
        except Exception as e:
            if again:  # back to how it was: last sentAt unchanged
                table.update_item(Key={"emailHash": h}, UpdateExpression="SET #s = :sent",
                                  ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":sent": "sent"})
            else:
                table.delete_item(Key={"emailHash": h})
            print(f"  FAILED {r['name']}: {type(e).__name__}; stopping")
            break
        table.update_item(Key={"emailHash": h}, UpdateExpression="SET #s = :s, sentAt = :t ADD sendCount :one",
                          ExpressionAttributeNames={"#s": "status"},
                          ExpressionAttributeValues={":s": "sent", ":t": int(time.time()), ":one": 1})
        table.update_item(Key={"emailHash": day_key}, UpdateExpression="ADD #c :one",
                          ExpressionAttributeNames={"#c": "count"}, ExpressionAttributeValues={":one": 1})
        sent += 1
        print(f"  sent {r['name']}" + (" (again)" if again else ""))
        time.sleep(SEND_GAP_SECONDS)
    print(f"sent {sent}")


def status(_args):
    table = boto3.resource("dynamodb", region_name=REGION).Table(RECRUIT_TABLE)
    counts, days, kw = {}, {}, {}
    while True:
        out = table.scan(**kw)
        for item in out["Items"]:
            if item["emailHash"].startswith("daily#"):
                days[item["emailHash"][6:]] = int(item.get("count", 0))
            else:
                counts[item.get("status", "?")] = counts.get(item.get("status", "?"), 0) + 1
        if "LastEvaluatedKey" not in out:
            break
        kw["ExclusiveStartKey"] = out["LastEvaluatedKey"]
    total = sum(counts.values())
    print(f"businesses emailed: {total}  " + "  ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    for d in sorted(days)[-14:]:
        print(f"  {d}  sent {days[d]}")


def address(args):
    ssm = boto3.client("ssm", region_name=REGION)
    if args.value:
        ssm.put_parameter(Name=ADDRESS_PARAM, Value=args.value.strip(), Type="String", Overwrite=True)
    print("postal address:", postal_address() or "(not set)")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("categories", help="list the categories and the Overture types in each")
    f = sub.add_parser("find", help="find businesses near a ZIP and save a CSV to review")
    f.add_argument("--zip", required=True)
    f.add_argument("--category", required=True, choices=list(CATEGORIES) + ["all"])
    f.add_argument("--miles", type=int, default=15)
    f.add_argument("--crawl", action="store_true",
                   help="visit websites: find missing emails, drop dead sites (slower)")
    sp = sub.add_parser("send", help="email the invite to the businesses in a reviewed CSV")
    sp.add_argument("csv", help="a reviewed recruit/*.csv, or ad.xlsx / seller.xlsx (emails in column A)")
    sp.add_argument("--yes", action="store_true", help="really send (without it: list who would get it)")
    sp.add_argument("--limit", type=int, default=DAILY_CAP, help=f"at most this many (daily cap {DAILY_CAP})")
    sp.add_argument("--to", help="send one preview of the first row to this address instead")
    sub.add_parser("status", help="how many businesses were emailed / unsubscribed, per day")
    ap = sub.add_parser("address", help="show or set the postal address in the email footer")
    ap.add_argument("value", nargs="?")
    args = p.parse_args()
    if args.cmd == "send":
        return send(args)
    if args.cmd == "status":
        return status(args)
    if args.cmd == "address":
        return address(args)
    if args.cmd == "categories":
        for c, types in CATEGORIES.items():
            print(f"{c:<10} ({EABU_CATEGORY[c]}): {', '.join(types)}")
    else:
        find(args)


if __name__ == "__main__":
    main()
