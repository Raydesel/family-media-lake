"""upload_trigger Lambda.

Fires on s3:ObjectCreated in the raw bucket. Parses the partitioned object
key, reads basic object metadata (size, content-type, uploader), and writes a
catalog item to DynamoDB. No AI / EXIF here -- that is Phase 2.

Handler pattern: validate event -> log -> business logic -> return response.

Expected key layout:
    raw/year=YYYY/month=MM/day=DD/<uuid>_<original_filename>
"""
from __future__ import annotations

import os
import re
import urllib.parse
from datetime import datetime, timezone
from typing import Any

import boto3

import log

# --- Configuration from environment (set by Terraform) ----------------------
CATALOG_TABLE = os.environ["CATALOG_TABLE"]
REGION = os.environ.get("AWS_REGION_") or os.environ.get("AWS_REGION", "us-east-1")

_dynamodb = boto3.resource("dynamodb", region_name=REGION)
_s3 = boto3.client("s3", region_name=REGION)
_table = _dynamodb.Table(CATALOG_TABLE)

# raw/year=2026/month=06/day=08/<uuid>_<name>
_KEY_RE = re.compile(
    r"^raw/year=(?P<year>\d{4})/month=(?P<month>\d{2})/day=(?P<day>\d{2})/"
    r"(?P<file_id>[0-9a-fA-F-]{36})_(?P<original_filename>.+)$"
)

_VIDEO_EXTS = {"mp4", "mov", "avi", "mkv", "webm", "m4v", "3gp", "mpg", "mpeg"}
_PHOTO_EXTS = {"jpg", "jpeg", "png", "gif", "heic", "heif", "webp", "tif", "tiff", "bmp", "dng"}


def parse_key(key: str) -> dict[str, Any]:
    """Pull partitions + identifiers out of a raw object key.

    Raises ValueError if the key does not match the expected layout so that
    stray uploads are surfaced loudly rather than silently miscataloged.
    """
    match = _KEY_RE.match(key)
    if not match:
        raise ValueError(f"key does not match expected layout: {key!r}")
    g = match.groupdict()
    return {
        "file_id": g["file_id"].lower(),
        "original_filename": g["original_filename"],
        "year": int(g["year"]),
        "month": int(g["month"]),
        "day": int(g["day"]),
        "s3_raw_key": key,
    }


def detect_media_type(filename: str, content_type: str | None) -> str:
    """Classify as 'photo' or 'video' from extension, falling back to MIME."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in _VIDEO_EXTS:
        return "video"
    if ext in _PHOTO_EXTS:
        return "photo"
    if content_type:
        if content_type.startswith("video/"):
            return "video"
        if content_type.startswith("image/"):
            return "photo"
    return "unknown"


def build_item(parsed: dict[str, Any], head: dict[str, Any]) -> dict[str, Any]:
    """Assemble the DynamoDB catalog item from parsed key + S3 HeadObject."""
    metadata = {k.lower(): v for k, v in (head.get("Metadata") or {}).items()}
    content_type = head.get("ContentType")
    now = datetime.now(timezone.utc).isoformat()

    return {
        "file_id": parsed["file_id"],
        "original_filename": parsed["original_filename"],
        "uploader": metadata.get("uploader", "unknown"),
        "upload_ts": now,
        "year": parsed["year"],
        "month": parsed["month"],
        "day": parsed["day"],
        "file_size_bytes": int(head.get("ContentLength", 0)),
        "media_type": detect_media_type(parsed["original_filename"], content_type),
        "content_type": content_type or "application/octet-stream",
        "s3_raw_key": parsed["s3_raw_key"],
        # Lifecycle marker the Phase 2 enrichment step will advance.
        "status": "uploaded",
    }


def _process_record(bucket: str, key: str) -> dict[str, Any]:
    parsed = parse_key(key)
    log.info("processing object", file_id=parsed["file_id"], s3_key=key)

    head = _s3.head_object(Bucket=bucket, Key=key)
    item = build_item(parsed, head)

    _table.put_item(Item=item)
    log.info(
        "catalog item written",
        file_id=item["file_id"],
        media_type=item["media_type"],
        file_size_bytes=item["file_size_bytes"],
        uploader=item["uploader"],
    )
    return item


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    # --- validate -----------------------------------------------------------
    records = (event or {}).get("Records") or []
    if not records:
        log.warning("event contained no S3 records", event_keys=list((event or {}).keys()))
        return {"processed": 0, "failed": 0}

    processed = 0
    failed = 0

    # --- business logic -----------------------------------------------------
    for record in records:
        s3 = record.get("s3", {})
        bucket = s3.get("bucket", {}).get("name")
        raw_key = s3.get("object", {}).get("key")
        if not bucket or not raw_key:
            log.warning("record missing bucket/key", record=record)
            failed += 1
            continue

        # S3 URL-encodes keys in event notifications.
        key = urllib.parse.unquote_plus(raw_key)
        try:
            _process_record(bucket, key)
            processed += 1
        except ValueError as exc:
            # Malformed key: log and skip (do not fail the whole batch).
            log.error("skipping malformed key", s3_key=key, error=str(exc))
            failed += 1
        except Exception as exc:  # noqa: BLE001 - surface + count, keep going
            log.error("failed to process record", s3_key=key, error=str(exc))
            failed += 1

    # --- response -----------------------------------------------------------
    log.info("batch complete", processed=processed, failed=failed)
    return {"processed": processed, "failed": failed}
