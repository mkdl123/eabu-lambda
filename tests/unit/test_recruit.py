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
