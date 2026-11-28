"""Seller recruitment: scripts/recruit.py send + the /guest/unsubscribe function."""
import csv
import email
import importlib.util
import os

import boto3
import pytest
from moto import mock_aws

from test_security import load, mk

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_recruit():
    spec = importlib.util.spec_from_file_location("recruit", os.path.join(ROOT, "scripts", "recruit.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.SEND_GAP_SECONDS = 0
    return m


def setup(tmp_path, rows):
    mk(boto3.resource("dynamodb", region_name="us-east-1"), "eabu-recruit", "emailHash", "S")
    ses = boto3.client("ses", region_name="us-east-1")
    ses.verify_domain_identity(Domain="invite.eabu.net")
    ses.verify_email_identity(EmailAddress="hello@invite.eabu.net")
    boto3.client("ssm", region_name="us-east-1").put_parameter(
        Name="/eabu/postal-address", Value="PO Box 1, Media, PA 19063", Type="String")
    path = tmp_path / "list.csv"
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["name", "category", "email", "city", "zip"])
        w.writeheader()
        w.writerows(rows)
    return str(path)


class Args:
    def __init__(self, path, yes=True, limit=50, to=None):
        self.csv, self.yes, self.limit, self.to = path, yes, limit, to


ROWS = [
    {"name": "Mayra's Cleaning <b>", "category": "cleaning", "email": "Mayra@MayrasCleans.com", "city": "WILMINGTON", "zip": "19801"},
    {"name": "Fit Co", "category": "fitness", "email": "fit@fitco.com", "city": "", "zip": "19060"},
    {"name": "Dup", "category": "fitness", "email": "fit@fitco.com", "city": "", "zip": "19060"},
    {"name": "No email", "category": "pets", "email": "", "city": "Media", "zip": "19063"},
]


def rows_in_table():
    t = boto3.resource("dynamodb", region_name="us-east-1").Table("eabu-recruit")
    return {i["emailHash"]: i for i in t.scan()["Items"]}


def test_render_escapes_html_and_names_service_city_address():
    r = load_recruit()
    subject, text, page = r.render(ROWS[0], "PO Box 1, Media, PA 19063")
    assert subject == "Customers near Wilmington for Mayra's Cleaning <b>"
    assert "Hi there," in text and "one of the first cleaning businesses" in text
    assert "People around Wilmington" in text and "PO Box 1, Media, PA 19063" in text
    assert "Mayra&#x27;s Cleaning &lt;b&gt;" in page
    assert r.UNSUBSCRIBE_URL.format(token=r.email_hash("mayra@mayrascleans.com")) in text


def test_render_without_city():
    subject, text, _ = load_recruit().render(ROWS[1], "x")
    assert subject == "Local customers for Fit Co" and "People around you have" in text


@mock_aws
def test_send_needs_postal_address(tmp_path):
    r = load_recruit()
    path = setup(tmp_path, ROWS)
    boto3.client("ssm", region_name="us-east-1").delete_parameter(Name="/eabu/postal-address")
    with pytest.raises(SystemExit):
        r.send(Args(path))
    assert rows_in_table() == {}


def test_footer_without_address_has_no_placeholder():
    _, text, page = load_recruit().render(ROWS[1], "")
    assert "eabu LLC\n" in text and "eabu LLC<br>" in page and "{" not in text


@mock_aws
def test_preview_allowed_without_address(tmp_path):
    r = load_recruit()
    path = setup(tmp_path, ROWS)
    boto3.client("ssm", region_name="us-east-1").delete_parameter(Name="/eabu/postal-address")
    r.send(Args(path, to="admin@eabu.net"))
    assert rows_in_table() == {}


@mock_aws
def test_dry_run_sends_nothing(tmp_path):
    r = load_recruit()
    r.send(Args(setup(tmp_path, ROWS), yes=False))
    assert rows_in_table() == {}


@mock_aws
def test_send_once_per_email_hash_only_and_daily_count(tmp_path):
    r = load_recruit()
    path = setup(tmp_path, ROWS)
    r.send(Args(path))
    items = rows_in_table()
    people = {k: v for k, v in items.items() if not k.startswith("daily#")}
    assert set(people) == {r.email_hash("mayra@mayrascleans.com"), r.email_hash("fit@fitco.com")}
    assert all(v["status"] == "sent" for v in people.values())
    assert "email" not in str(people).replace("emailHash", "")  # no plaintext email stored
    assert [int(v["count"]) for k, v in items.items() if k.startswith("daily#")] == [2]
    r.send(Args(path))  # second run: nobody new
    assert [int(v["count"]) for k, v in rows_in_table().items() if k.startswith("daily#")] == [2]


@mock_aws
def test_daily_cap(tmp_path):
    r = load_recruit()
    r.DAILY_CAP = 1
    r.send(Args(setup(tmp_path, ROWS)))
    assert len([k for k in rows_in_table() if not k.startswith("daily#")]) == 1


@mock_aws
def test_message_headers(tmp_path):
    r = load_recruit()
    subject, text, page = r.render(ROWS[1], "addr")
    msg = email.message_from_bytes(r.build_message("fit@fitco.com", subject, text, page, "https://u").as_bytes())
    assert msg["From"] == "Muk at eabu <hello@invite.eabu.net>" and msg["Reply-To"] == "admin@eabu.net"
    assert msg["List-Unsubscribe"] == "<https://u>"
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert msg.get_content_type() == "multipart/alternative"


@mock_aws
def test_preview_records_nothing(tmp_path):
    r = load_recruit()
    r.send(Args(setup(tmp_path, ROWS), to="admin@eabu.net"))
    assert rows_in_table() == {}


@mock_aws
def test_unsubscribe_marks_only_existing_rows(tmp_path):
    r = load_recruit()
    path = setup(tmp_path, ROWS)
    r.send(Args(path))
    fn = load("eabu-recruit-unsubscribe")
    h = r.email_hash("fit@fitco.com")

    res = fn.lambda_handler({"httpMethod": "GET", "queryStringParameters": {"t": h}}, None)
    assert res["statusCode"] == 200 and "unsubscribed" in res["body"]
    assert rows_in_table()[h]["status"] == "unsubscribed"

    unknown = "a" * 64
    res = fn.lambda_handler({"httpMethod": "POST", "queryStringParameters": {"t": unknown}}, None)
    assert res["statusCode"] == 200 and unknown not in rows_in_table()  # same answer, no row made

    for bad in [None, {}, {"t": "xyz"}, {"t": "g" * 64}]:
        res = fn.lambda_handler({"httpMethod": "GET", "queryStringParameters": bad}, None)
        assert res["statusCode"] == 400

    # an unsubscribed business is never emailed again
    boto3.resource("dynamodb", region_name="us-east-1").Table("eabu-recruit").delete_item(
        Key={"emailHash": r.email_hash("mayra@mayrascleans.com")})
    r.send(Args(path))
    assert rows_in_table()[h]["status"] == "unsubscribed"


def write_xlsx(path, cells):
    """Minimal .xlsx: column A = cells (shared strings), plus a B column that must be ignored."""
    import zipfile
    m = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    strings = "".join(f"<si><t>{c}</t></si>" for c in cells)
    rows = "".join(f'<row r="{i}"><c r="A{i}" t="s"><v>{i - 1}</v></c>'
                   f'<c r="B{i}" t="inlineStr"><is><t>other{i}@b.com</t></is></c></row>'
                   for i in range(1, len(cells) + 1))
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook xmlns="{m}" xmlns:r="http://schemas.openxmlformats.org/'
                   'officeDocument/2006/relationships"><sheets><sheet name="S" sheetId="1" r:id="rId1"/>'
                   '</sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/'
                   'package/2006/relationships"><Relationship Id="rId1" Type="x" '
                   'Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr("xl/sharedStrings.xml", f'<sst xmlns="{m}">{strings}</sst>')
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{m}"><sheetData>{rows}</sheetData></worksheet>')


PAGE = """<!doctype html><html><head><title>Advertise on eabu</title></head><body>
<table id="email"><tr><td><p>Hi there,</p></td></tr>
<tr><td><a href="mailto:admin@eabu.net?subject=Unsubscribe">Unsubscribe</a> ·
eabu LLC, 600 N. 2nd Street, Harrisburg, PA 17101<br></td></tr></table>
<!-- EDITOR (only works in a browser) -->
<div id="eabu-editor-bar"><span>edit</span></div>
<script id="eabu-editor-script">alert(1)</script>
</body></html>"""


def test_xlsx_column_a_picks_template_and_sends_page(tmp_path):
    r = load_recruit()
    (tmp_path / "marketing-email-business.html").write_text(PAGE)
    xlsx = tmp_path / "ad.xlsx"
    write_xlsx(xlsx, ["Email", "Joe@Shop.com ", "amy@bakery.net", "joe@shop.com"])
    rows = r.read_rows(str(xlsx))
    assert [x["email"] for x in rows] == ["joe@shop.com", "amy@bakery.net"]
    assert r.xlsx_template(str(xlsx))[1].endswith("marketing-email-business.html")
    assert r.xlsx_template(str(tmp_path / "seller-miami.xlsx"))[0] == "seller"
    with pytest.raises(SystemExit):
        r.xlsx_template(str(tmp_path / "foo.xlsx"))

    subject, text, page = r.render_page(str(tmp_path / "marketing-email-business.html"),
                                        "https://api.eabu.net/guest/unsubscribe?t=abc")
    assert subject == "Advertise on eabu"
    assert "eabu-editor" not in page and "alert(1)" not in page and "mailto:" not in page
    assert "guest/unsubscribe?t=abc" in page and "guest/unsubscribe?t=abc" in text
    assert "Harrisburg, PA 17101" in text


def test_daily_cap_day_is_us_eastern():
    from datetime import datetime, timezone
    r = load_recruit()
    # Friday 8 pm Eastern is already Saturday in UTC, but counts for Friday
    assert r.send_day(datetime(2026, 11, 28, 1, 0, tzinfo=timezone.utc)) == "2026-11-27"
    assert r.send_day(datetime(2026, 11, 28, 5, 30, tzinfo=timezone.utc)) == "2026-11-28"
    assert r.send_day(datetime(2026, 7, 4, 3, 59, tzinfo=timezone.utc)) == "2026-07-03"  # EDT


@mock_aws
def test_sheet_resends_only_after_30_days_and_never_after_unsubscribe(tmp_path):
    r = load_recruit()
    setup(tmp_path, [])
    (tmp_path / "marketing-email-business.html").write_text(PAGE)
    xlsx = tmp_path / "ad.xlsx"
    write_xlsx(xlsx, ["a@shop.com", "b@shop.com", "c@shop.com"])
    table = boto3.resource("dynamodb", region_name="us-east-1").Table("eabu-recruit")
    a, b, c = (r.email_hash(e) for e in ("a@shop.com", "b@shop.com", "c@shop.com"))
    r.send(Args(str(xlsx)))
    first = rows_in_table()
    assert all(first[h]["status"] == "sent" and int(first[h]["sendCount"]) == 1 for h in (a, b, c))

    r.send(Args(str(xlsx)))  # same day: nobody again
    assert all(int(rows_in_table()[h]["sendCount"]) == 1 for h in (a, b, c))

    old = int(first[a]["sentAt"]) - 30 * 86400
    table.update_item(Key={"emailHash": a}, UpdateExpression="SET sentAt = :t", ExpressionAttributeValues={":t": old})
    table.update_item(Key={"emailHash": b}, UpdateExpression="SET sentAt = :t, #s = :u",
                      ExpressionAttributeNames={"#s": "status"},
                      ExpressionAttributeValues={":t": old, ":u": "unsubscribed"})
    table.update_item(Key={"emailHash": c}, UpdateExpression="SET sentAt = :t",
                      ExpressionAttributeValues={":t": old + 3600})  # 29 days 23 hours ago
    r.send(Args(str(xlsx)))
    now = rows_in_table()
    assert int(now[a]["sendCount"]) == 2 and int(now[a]["sentAt"]) > old  # 30 days: sent again
    assert now[b]["status"] == "unsubscribed" and "sendCount" in now[b] and int(now[b]["sendCount"]) == 1
    assert int(now[c]["sendCount"]) == 1  # not yet 30 days

    # CSV invites never repeat, however old
    path = str(tmp_path / "list.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["name", "category", "email", "city", "zip"])
        w.writeheader()
        w.writerows(ROWS)
    r.send(Args(path))
    h = r.email_hash("fit@fitco.com")
    table.update_item(Key={"emailHash": h}, UpdateExpression="SET sentAt = :t", ExpressionAttributeValues={":t": 1})
    r.send(Args(path))
    assert int(rows_in_table()[h]["sendCount"]) == 1
