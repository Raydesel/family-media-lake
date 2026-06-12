"""Unit tests for the pre-signed URL generator script (pure-logic parts)."""
from datetime import datetime, timezone

import generate_upload_url as g


def test_build_key_layout():
    when = datetime(2026, 6, 8, tzinfo=timezone.utc)
    key = g.build_key("beach.jpg", when, "3f2504e0-4f89-41d3-9a0c-0305e82c3301")
    assert key == "raw/year=2026/month=06/day=08/3f2504e0-4f89-41d3-9a0c-0305e82c3301_beach.jpg"


def test_build_key_sanitizes_spaces_and_strips_dirs():
    when = datetime(2026, 1, 2, tzinfo=timezone.utc)
    key = g.build_key("/tmp/My Holiday.jpg", when, "abc")
    assert key == "raw/year=2026/month=01/day=02/abc_My_Holiday.jpg"


def test_key_roundtrips_through_handler_parser():
    """A key produced by the script must be parseable by the Lambda."""
    import handler

    when = datetime(2026, 12, 25, tzinfo=timezone.utc)
    key = g.build_key("xmas.png", when, "3f2504e0-4f89-41d3-9a0c-0305e82c3301")
    parsed = handler.parse_key(key)
    assert parsed["original_filename"] == "xmas.png"
    assert parsed["year"] == 2026 and parsed["month"] == 12 and parsed["day"] == 25
