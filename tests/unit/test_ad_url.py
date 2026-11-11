import importlib.util
import os
import sys

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "eabu-dish", "python"))
_spec = importlib.util.spec_from_file_location(
    "ad_post_app", os.path.join(os.path.dirname(__file__), "..", "..", "eabu-dish", "eabu-advertisement-post", "app.py"))
app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(app)


@pytest.mark.parametrize("raw,expected", [
    ("", ""),
    ("www.shop.com", "https://www.shop.com"),
    ("shop.com/menu", "https://shop.com/menu"),
    ("  https://shop.com ", "https://shop.com"),
])
def test_url_gets_https(raw, expected):
    assert app._clean_url(raw) == expected


@pytest.mark.parametrize("raw", ["http://shop.com", "ftp://shop.com", "www.shop .com"])
def test_bad_url_rejected(raw):
    with pytest.raises(ValueError):
        app._clean_url(raw)


@pytest.mark.parametrize("raw", ["javascript:alert(1)", "data:text/html,x", "mailto:a@b.com"])
def test_other_schemes_not_prefixed(raw):
    with pytest.raises(ValueError):
        app._clean_url(raw)
