"""Shared fixtures. moto has no Rekognition DetectModerationLabels, so a fake client stands in."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "eabu-dish", "python"))

from my_utils import content_filter  # noqa: E402


class FakeRekognition:
    """Returns the labels a test sets (none by default, i.e. every picture is clean)."""

    def __init__(self):
        self.labels = []
        self.error = None
        self.calls = 0

    def detect_moderation_labels(self, Image, MinConfidence):
        self.calls += 1
        assert Image["Bytes"], "the picture's bytes are sent"
        if self.error:
            raise self.error
        return {"ModerationLabels": self.labels}


@pytest.fixture(autouse=True)
def rekognition(monkeypatch):
    fake = FakeRekognition()
    monkeypatch.setattr(content_filter, "_rekognition", fake)
    return fake
