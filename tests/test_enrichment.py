"""Unit tests for the enrichment Lambda."""
import io
import json
from datetime import datetime, timezone
from decimal import Decimal

import pyarrow.parquet as pq
import pytest
from PIL import Image

import enrichment_handler as eh


# --- EXIF helpers -------------------------------------------------------------

def test_dms_to_degrees_north():
    # 40 deg 26' 46.3" N
    assert eh.dms_to_degrees(((40, 1), (26, 1), (463, 10)), "N") == pytest.approx(40.4462, abs=1e-3)


def test_dms_to_degrees_southern_hemisphere_is_negative():
    deg = eh.dms_to_degrees(((33, 1), (52, 1), (0, 1)), "S")
    assert deg == pytest.approx(-33.8667, abs=1e-3)


def test_dms_to_degrees_west_is_negative():
    assert eh.dms_to_degrees(((118, 1), (0, 1), (0, 1)), "W") == pytest.approx(-118.0)


def test_dms_to_degrees_handles_garbage():
    assert eh.dms_to_degrees(None, "N") is None
    assert eh.dms_to_degrees((1,), "N") is None
    assert eh.dms_to_degrees(("x", "y", "z"), "N") is None


def test_parse_exif_ts():
    ts = eh.parse_exif_ts("2024:07:15 18:30:05")
    assert ts == datetime(2024, 7, 15, 18, 30, 5, tzinfo=timezone.utc)


def test_parse_exif_ts_bytes_and_garbage():
    assert eh.parse_exif_ts(b"2024:01/bad") is None
    assert eh.parse_exif_ts(None) is None
    assert eh.parse_exif_ts("") is None


def test_extract_exif_image_without_exif_returns_nulls():
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buf, format="JPEG")
    out = eh.extract_exif(buf.getvalue())
    assert out == {"capture_ts": None, "gps_lat": None, "gps_lon": None}


def test_extract_exif_corrupt_bytes_returns_nulls():
    out = eh.extract_exif(b"definitely not an image")
    assert out == {"capture_ts": None, "gps_lat": None, "gps_lon": None}


# --- Thumbnail ------------------------------------------------------------------

def test_make_thumbnail_downscales_and_is_jpeg():
    buf = io.BytesIO()
    Image.new("RGB", (2000, 1000), "blue").save(buf, format="JPEG")
    thumb = eh.make_thumbnail(buf.getvalue(), max_px=512)
    img = Image.open(io.BytesIO(thumb))
    assert img.format == "JPEG"
    assert max(img.size) <= 512


def test_make_thumbnail_converts_rgba_png():
    buf = io.BytesIO()
    Image.new("RGBA", (100, 100), (255, 0, 0, 128)).save(buf, format="PNG")
    thumb = eh.make_thumbnail(buf.getvalue())
    assert Image.open(io.BytesIO(thumb)).mode == "RGB"


# --- Video metadata -------------------------------------------------------------

def test_parse_video_ts_iso_z():
    ts = eh.parse_video_ts("2012-06-24T16:26:38.000000Z")
    assert ts == datetime(2012, 6, 24, 16, 26, 38, tzinfo=timezone.utc)


def test_parse_video_ts_garbage():
    assert eh.parse_video_ts("") is None
    assert eh.parse_video_ts(None) is None


def test_parse_ffprobe_creation_time_from_format_tags():
    payload = {
        "format": {"tags": {"creation_time": "2012-06-24T16:26:38.000000Z"}},
        "streams": [],
    }
    ts = eh.parse_ffprobe_creation_time(payload)
    assert ts == datetime(2012, 6, 24, 16, 26, 38, tzinfo=timezone.utc)


def test_parse_ffprobe_creation_time_falls_back_to_stream_tags():
    payload = {
        "format": {"tags": {}},
        "streams": [{"tags": {"creation_time": "2014-01-15T10:00:00.000000Z"}}],
    }
    ts = eh.parse_ffprobe_creation_time(payload)
    assert ts.year == 2014


# --- Rekognition transforms -------------------------------------------------------

def test_extract_labels():
    resp = {"Labels": [{"Name": "Beach", "Confidence": 99.1}, {"Name": "Person"}]}
    assert eh.extract_labels(resp) == ["Beach", "Person"]
    assert eh.extract_labels({}) == []


def test_extract_faces_picks_top_emotion():
    resp = {
        "FaceDetails": [
            {
                "Confidence": 99.9,
                "AgeRange": {"Low": 25, "High": 35},
                "Emotions": [
                    {"Type": "CALM", "Confidence": 60.0},
                    {"Type": "HAPPY", "Confidence": 90.0},
                ],
            }
        ]
    }
    faces = eh.extract_faces(resp)
    assert len(faces) == 1
    assert faces[0]["emotion"] == "HAPPY"
    assert faces[0]["age_low"] == 25 and faces[0]["age_high"] == 35
    assert len(faces[0]["face_id"]) == 36  # uuid placeholder


def test_extract_faces_no_emotions():
    faces = eh.extract_faces({"FaceDetails": [{"Confidence": 88.0}]})
    assert faces[0]["emotion"] is None


# --- Parquet ----------------------------------------------------------------------

def _record(**overrides):
    base = {
        "file_id": "3f2504e0-4f89-41d3-9a0c-0305e82c3301",
        "original_filename": "beach.jpg",
        "uploader": "family",
        "upload_ts": datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc),
        "capture_ts": datetime(2026, 6, 7, 9, 30, tzinfo=timezone.utc),
        "gps_lat": 40.4462,
        "gps_lon": -3.7492,
        "location_name": None,
        "rekognition_labels": ["Beach", "Person"],
        "rekognition_faces": [
            {"face_id": "abc", "confidence": 99.9, "age_low": 25, "age_high": 35, "emotion": "HAPPY"}
        ],
        "file_size_bytes": 12345,
        "media_type": "photo",
        "s3_raw_key": "raw/year=2026/month=06/day=08/x_beach.jpg",
        "s3_thumbnail_key": "thumbnails/year=2026/month=06/day=08/x.jpg",
        "album_ids": [],
    }
    base.update(overrides)
    return base


def test_parquet_roundtrip():
    data = eh.build_parquet_bytes(_record())
    table = pq.read_table(io.BytesIO(data))
    assert table.num_rows == 1
    row = table.to_pylist()[0]
    assert row["file_id"] == "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
    assert row["rekognition_labels"] == ["Beach", "Person"]
    assert row["rekognition_faces"][0]["emotion"] == "HAPPY"
    assert row["gps_lon"] == pytest.approx(-3.7492)
    assert row["album_ids"] == []


def test_parquet_roundtrip_with_nulls_for_video():
    data = eh.build_parquet_bytes(
        _record(
            capture_ts=None,
            gps_lat=None,
            gps_lon=None,
            rekognition_labels=[],
            rekognition_faces=[],
            s3_thumbnail_key=None,
            media_type="video",
        )
    )
    row = pq.read_table(io.BytesIO(data)).to_pylist()[0]
    assert row["capture_ts"] is None
    assert row["gps_lat"] is None
    assert row["media_type"] == "video"


def test_partition_prefix_zero_pads():
    assert eh.partition_prefix(2026, 6, 8) == "year=2026/month=06/day=08"


# --- Handler ------------------------------------------------------------------------

class _FakeS3:
    def __init__(self, body=b""):
        self.body = body
        self.put_calls = []

    def get_object(self, Bucket, Key):  # noqa: N803
        return {"Body": io.BytesIO(self.body)}

    def download_file(self, Bucket, Key, Filename):  # noqa: N803
        from pathlib import Path

        Path(Filename).write_bytes(self.body)

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)
        return {}


class _FakeRekognition:
    def detect_labels(self, **kwargs):
        return {"Labels": [{"Name": "Beach"}]}

    def detect_faces(self, **kwargs):
        return {"FaceDetails": [{"Confidence": 99.0, "AgeRange": {"Low": 20, "High": 30}}]}


class _FakeTable:
    def __init__(self):
        self.updates = []

    def update_item(self, **kwargs):
        self.updates.append(kwargs)


def _jpeg_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), "green").save(buf, format="JPEG")
    return buf.getvalue()


def _stream_event(media_type="photo", status="uploaded", event_name="INSERT"):
    return {
        "Records": [
            {
                "eventName": event_name,
                "dynamodb": {
                    "SequenceNumber": "100",
                    "NewImage": {
                        "file_id": {"S": "3f2504e0-4f89-41d3-9a0c-0305e82c3301"},
                        "original_filename": {"S": "beach.jpg"},
                        "uploader": {"S": "family"},
                        "upload_ts": {"S": "2026-06-08T12:00:00+00:00"},
                        "year": {"N": "2026"},
                        "month": {"N": "6"},
                        "day": {"N": "8"},
                        "file_size_bytes": {"N": "12345"},
                        "media_type": {"S": media_type},
                        "s3_raw_key": {"S": "raw/year=2026/month=06/day=08/3f2504e0-4f89-41d3-9a0c-0305e82c3301_beach.jpg"},
                        "status": {"S": status},
                    },
                },
            }
        ]
    }


@pytest.fixture
def fakes(monkeypatch):
    s3 = _FakeS3(body=_jpeg_bytes())
    rek = _FakeRekognition()
    table = _FakeTable()
    monkeypatch.setattr(eh, "_s3", s3)
    monkeypatch.setattr(eh, "_rekognition", rek)
    monkeypatch.setattr(eh, "_table", table)
    return s3, rek, table


def test_handler_photo_full_pipeline(fakes):
    s3, _, table = fakes
    result = eh.handler(_stream_event(), None)
    assert result == {"batchItemFailures": []}

    keys = [c["Key"] for c in s3.put_calls]
    assert "thumbnails/year=2026/month=06/day=08/3f2504e0-4f89-41d3-9a0c-0305e82c3301.jpg" in keys
    assert "metadata/year=2026/month=06/day=08/3f2504e0-4f89-41d3-9a0c-0305e82c3301.parquet" in keys

    # Parquet content is valid and carries the rekognition labels.
    parquet_call = next(c for c in s3.put_calls if c["Key"].endswith(".parquet"))
    row = pq.read_table(io.BytesIO(parquet_call["Body"])).to_pylist()[0]
    assert row["rekognition_labels"] == ["Beach"]
    assert len(row["rekognition_faces"]) == 1

    # Catalog flipped to enriched.
    assert len(table.updates) == 1
    values = table.updates[0]["ExpressionAttributeValues"]
    assert "enriched" in values.values()


def test_handler_video_extracts_capture_ts_and_poster(fakes, monkeypatch):
    s3, _, table = fakes
    capture = datetime(2012, 6, 24, 16, 26, 38, tzinfo=timezone.utc)
    monkeypatch.setattr(eh, "extract_video_creation_time", lambda _path: capture)
    monkeypatch.setattr(eh, "extract_video_poster", lambda _path, max_px: _jpeg_bytes())

    result = eh.handler(_stream_event(media_type="video"), None)
    assert result == {"batchItemFailures": []}

    keys = [c["Key"] for c in s3.put_calls]
    assert "thumbnails/year=2026/month=06/day=08/3f2504e0-4f89-41d3-9a0c-0305e82c3301.jpg" in keys
    parquet_call = next(c for c in s3.put_calls if c["Key"].endswith(".parquet"))
    row = pq.read_table(io.BytesIO(parquet_call["Body"])).to_pylist()[0]
    assert row["rekognition_labels"] == []
    assert row["capture_ts"] == capture
    assert row["s3_thumbnail_key"] is not None

    values = table.updates[0]["ExpressionAttributeValues"]
    assert capture.isoformat() in {v for v in values.values() if isinstance(v, str)}


def test_handler_ignores_non_insert_and_wrong_status(fakes):
    s3, _, table = fakes
    eh.handler(_stream_event(event_name="MODIFY"), None)
    eh.handler(_stream_event(status="enriched"), None)
    assert s3.put_calls == []
    assert table.updates == []


def test_handler_reports_failed_sequence_numbers(fakes, monkeypatch):
    def boom(item):
        raise RuntimeError("kaput")

    monkeypatch.setattr(eh, "process_item", boom)
    result = eh.handler(_stream_event(), None)
    assert result == {"batchItemFailures": [{"itemIdentifier": "100"}]}


def test_handler_empty_event():
    assert eh.handler({}, None) == {"batchItemFailures": []}
