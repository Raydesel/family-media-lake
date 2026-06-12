"""Unit tests for the upload_trigger Lambda."""
import pytest

import handler


# --- parse_key --------------------------------------------------------------

def test_parse_key_happy_path():
    key = "raw/year=2026/month=06/day=08/3f2504e0-4f89-41d3-9a0c-0305e82c3301_beach.jpg"
    parsed = handler.parse_key(key)
    assert parsed["file_id"] == "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
    assert parsed["original_filename"] == "beach.jpg"
    assert parsed["year"] == 2026
    assert parsed["month"] == 6
    assert parsed["day"] == 8
    assert parsed["s3_raw_key"] == key


def test_parse_key_filename_with_underscores():
    key = "raw/year=2026/month=01/day=02/3f2504e0-4f89-41d3-9a0c-0305e82c3301_my_holiday_pic.jpeg"
    parsed = handler.parse_key(key)
    assert parsed["original_filename"] == "my_holiday_pic.jpeg"


@pytest.mark.parametrize(
    "bad_key",
    [
        "uploads/2026/06/08/abc_file.jpg",          # wrong prefix/layout
        "raw/year=2026/month=6/day=8/uuid_file.jpg",  # unpadded month/day
        "raw/year=2026/month=06/day=08/notauuid_file.jpg",  # bad uuid
    ],
)
def test_parse_key_rejects_malformed(bad_key):
    with pytest.raises(ValueError):
        handler.parse_key(bad_key)


# --- detect_media_type ------------------------------------------------------

@pytest.mark.parametrize(
    "name,ctype,expected",
    [
        ("a.jpg", None, "photo"),
        ("a.HEIC", None, "photo"),
        ("clip.mp4", None, "video"),
        ("clip.MOV", None, "video"),
        ("noext", "image/png", "photo"),
        ("noext", "video/mp4", "video"),
        ("mystery.xyz", None, "unknown"),
    ],
)
def test_detect_media_type(name, ctype, expected):
    assert handler.detect_media_type(name, ctype) == expected


# --- build_item -------------------------------------------------------------

def test_build_item_uses_head_metadata():
    parsed = handler.parse_key(
        "raw/year=2026/month=06/day=08/3f2504e0-4f89-41d3-9a0c-0305e82c3301_beach.jpg"
    )
    head = {
        "ContentLength": 1234,
        "ContentType": "image/jpeg",
        "Metadata": {"uploader": "family"},
    }
    item = handler.build_item(parsed, head)
    assert item["file_id"] == "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
    assert item["uploader"] == "family"
    assert item["file_size_bytes"] == 1234
    assert item["media_type"] == "photo"
    assert item["status"] == "uploaded"
    assert item["upload_ts"].endswith("+00:00")


def test_build_item_defaults_uploader_unknown():
    parsed = handler.parse_key(
        "raw/year=2026/month=06/day=08/3f2504e0-4f89-41d3-9a0c-0305e82c3301_beach.jpg"
    )
    item = handler.build_item(parsed, {"ContentLength": 0, "Metadata": {}})
    assert item["uploader"] == "unknown"


# --- handler ----------------------------------------------------------------

class _FakeTable:
    def __init__(self):
        self.items = []

    def put_item(self, Item):  # noqa: N803 - boto3 kwarg name
        self.items.append(Item)


class _FakeS3:
    def __init__(self, head):
        self._head = head
        self.calls = []

    def head_object(self, Bucket, Key):  # noqa: N803 - boto3 kwarg names
        self.calls.append((Bucket, Key))
        return self._head


def _s3_event(key, bucket="family-media-raw-123"):
    return {"Records": [{"s3": {"bucket": {"name": bucket}, "object": {"key": key}}}]}


def test_handler_writes_catalog_item(monkeypatch):
    fake_table = _FakeTable()
    fake_s3 = _FakeS3({"ContentLength": 999, "ContentType": "image/jpeg", "Metadata": {"uploader": "mom"}})
    monkeypatch.setattr(handler, "_table", fake_table)
    monkeypatch.setattr(handler, "_s3", fake_s3)

    event = _s3_event("raw/year=2026/month=06/day=08/3f2504e0-4f89-41d3-9a0c-0305e82c3301_beach.jpg")
    result = handler.handler(event, None)

    assert result == {"processed": 1, "failed": 0}
    assert len(fake_table.items) == 1
    assert fake_table.items[0]["uploader"] == "mom"
    assert fake_table.items[0]["file_size_bytes"] == 999


def test_handler_urldecodes_key(monkeypatch):
    fake_table = _FakeTable()
    fake_s3 = _FakeS3({"ContentLength": 1, "ContentType": "image/jpeg", "Metadata": {}})
    monkeypatch.setattr(handler, "_table", fake_table)
    monkeypatch.setattr(handler, "_s3", fake_s3)

    # S3 encodes spaces as '+'
    encoded = "raw/year=2026/month=06/day=08/3f2504e0-4f89-41d3-9a0c-0305e82c3301_my+beach+pic.jpg"
    handler.handler(_s3_event(encoded), None)

    assert fake_s3.calls[0][1].endswith("my beach pic.jpg")
    assert fake_table.items[0]["original_filename"] == "my beach pic.jpg"


def test_handler_skips_malformed_key_without_failing_batch(monkeypatch):
    fake_table = _FakeTable()
    fake_s3 = _FakeS3({"ContentLength": 1, "Metadata": {}})
    monkeypatch.setattr(handler, "_table", fake_table)
    monkeypatch.setattr(handler, "_s3", fake_s3)

    result = handler.handler(_s3_event("raw/garbage/key.jpg"), None)
    assert result == {"processed": 0, "failed": 1}
    assert fake_table.items == []


def test_handler_empty_event():
    assert handler.handler({}, None) == {"processed": 0, "failed": 0}
