"""enrichment Lambda (Phase 2).

Triggered by the DynamoDB catalog table's stream (INSERT events written by
upload_trigger). For each new media item:

  1. Photos: download original, extract EXIF (capture_ts + GPS) with Pillow.
  2. Photos: generate a JPEG thumbnail -> processed bucket.
  3. Photos: Rekognition DetectLabels + DetectFaces (by S3 reference, no
     re-upload). Videos skip Rekognition (video APIs are async + pricier;
     deferred to a later phase).
  4. All media: write a one-row Parquet file with the full metadata schema to
     processed/metadata/year=.../month=.../day=.../<uuid>.parquet (the Glue
     table over this prefix uses partition projection, so no crawler runs).
  5. Update the catalog item: status=enriched + denormalized highlights.

Handler pattern: validate event -> log -> business logic -> return response.
Partial-batch failures are reported back to the stream so only failed records
are retried.
"""
from __future__ import annotations

import io
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import boto3
import pyarrow as pa
import pyarrow.parquet as pq
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError
from PIL import ExifTags, Image, ImageOps

# --- Configuration from environment (set by Terraform) ----------------------
RAW_BUCKET = os.environ["RAW_BUCKET"]
PROCESSED_BUCKET = os.environ["PROCESSED_BUCKET"]
CATALOG_TABLE = os.environ["CATALOG_TABLE"]
THUMBNAIL_MAX_PX = int(os.environ.get("THUMBNAIL_MAX_PX", "512"))
MAX_LABELS = int(os.environ.get("MAX_LABELS", "25"))
MIN_LABEL_CONFIDENCE = float(os.environ.get("MIN_LABEL_CONFIDENCE", "80"))

_s3 = boto3.client("s3")
_rekognition = boto3.client("rekognition")
_table = boto3.resource("dynamodb").Table(CATALOG_TABLE)
_deserializer = TypeDeserializer()


# --- Structured logging ------------------------------------------------------

def _log(level: str, message: str, **fields) -> None:
    record = {"level": level, "function": "enrichment", "message": message}
    record.update({k: v for k, v in fields.items() if v is not None})
    stream = sys.stderr if level in ("ERROR", "WARNING") else sys.stdout
    print(json.dumps(record, default=str), file=stream)


def info(message: str, **fields) -> None:
    _log("INFO", message, **fields)


def warning(message: str, **fields) -> None:
    _log("WARNING", message, **fields)


def error(message: str, **fields) -> None:
    _log("ERROR", message, **fields)


# --- Parquet schema (mirrors the Glue table; year/month/day live in the path)

PARQUET_SCHEMA = pa.schema(
    [
        ("file_id", pa.string()),
        ("original_filename", pa.string()),
        ("uploader", pa.string()),
        ("upload_ts", pa.timestamp("ms", tz="UTC")),
        ("capture_ts", pa.timestamp("ms", tz="UTC")),
        ("gps_lat", pa.float64()),
        ("gps_lon", pa.float64()),
        ("location_name", pa.string()),
        ("rekognition_labels", pa.list_(pa.string())),
        (
            "rekognition_faces",
            pa.list_(
                pa.struct(
                    [
                        ("face_id", pa.string()),
                        ("confidence", pa.float64()),
                        ("age_low", pa.int32()),
                        ("age_high", pa.int32()),
                        ("emotion", pa.string()),
                    ]
                )
            ),
        ),
        ("file_size_bytes", pa.int64()),
        ("media_type", pa.string()),
        ("s3_raw_key", pa.string()),
        ("s3_thumbnail_key", pa.string()),
        ("album_ids", pa.list_(pa.string())),
    ]
)


# --- EXIF helpers -------------------------------------------------------------

def _norm_ref(ref: Any) -> str | None:
    if isinstance(ref, bytes):
        ref = ref.decode(errors="ignore")
    return ref or None


def _ratio(value: Any) -> float:
    # Old Pillow versions hand back (numerator, denominator) tuples;
    # newer ones return IFDRational, which float() handles directly.
    if isinstance(value, tuple):
        return float(value[0]) / float(value[1])
    return float(value)


def dms_to_degrees(dms: Any, ref: str | None) -> float | None:
    """Convert EXIF (degrees, minutes, seconds) + hemisphere ref to a float."""
    if not dms or len(dms) < 3:
        return None
    try:
        degrees = _ratio(dms[0]) + _ratio(dms[1]) / 60.0 + _ratio(dms[2]) / 3600.0
    except (ValueError, TypeError, ZeroDivisionError):
        return None
    if ref in ("S", "W"):
        degrees = -degrees
    return round(degrees, 7)


def parse_exif_ts(value: Any) -> datetime | None:
    """Parse an EXIF 'YYYY:MM:DD HH:MM:SS' timestamp.

    EXIF carries no timezone; we record it as UTC rather than guessing.
    """
    if isinstance(value, bytes):
        value = value.decode(errors="ignore")
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip(), "%Y:%m:%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def extract_exif(data: bytes) -> dict[str, Any]:
    """Pull capture timestamp and GPS coordinates out of image bytes."""
    out: dict[str, Any] = {"capture_ts": None, "gps_lat": None, "gps_lon": None}
    try:
        img = Image.open(io.BytesIO(data))
        exif = img.getexif()
    except Exception as exc:  # e.g. HEIC without a plugin, corrupt file
        warning("could not read image for EXIF", error=str(exc))
        return out
    if not exif:
        return out

    exif_ifd = exif.get_ifd(ExifTags.IFD.Exif) or {}
    out["capture_ts"] = parse_exif_ts(exif_ifd.get(0x9003) or exif.get(0x0132))

    try:
        gps = exif.get_ifd(ExifTags.IFD.GPSInfo) or {}
    except Exception:
        gps = {}
    out["gps_lat"] = dms_to_degrees(gps.get(2), _norm_ref(gps.get(1)))
    out["gps_lon"] = dms_to_degrees(gps.get(4), _norm_ref(gps.get(3)))
    return out


# --- Thumbnail ----------------------------------------------------------------

def make_thumbnail(data: bytes, max_px: int = 512) -> bytes:
    """Downscale to a JPEG thumbnail, honoring EXIF orientation."""
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    img.thumbnail((max_px, max_px))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85, optimize=True)
    return buf.getvalue()


# --- Rekognition --------------------------------------------------------------

def extract_labels(response: dict[str, Any]) -> list[str]:
    return [l["Name"] for l in response.get("Labels", []) if "Name" in l]


def extract_faces(response: dict[str, Any]) -> list[dict[str, Any]]:
    faces = []
    for detail in response.get("FaceDetails", []):
        emotions = detail.get("Emotions") or []
        top_emotion = (
            max(emotions, key=lambda e: e.get("Confidence", 0.0)).get("Type")
            if emotions
            else None
        )
        faces.append(
            {
                # Placeholder identity; Phase 3 IndexFaces will assign stable
                # collection face ids for clustering.
                "face_id": str(uuid.uuid4()),
                "confidence": float(detail.get("Confidence", 0.0)),
                "age_low": int(detail.get("AgeRange", {}).get("Low", 0)),
                "age_high": int(detail.get("AgeRange", {}).get("High", 0)),
                "emotion": top_emotion,
            }
        )
    return faces


def run_rekognition(raw_key: str, file_id: str) -> tuple[list[str], list[dict[str, Any]]]:
    """DetectLabels + DetectFaces by S3 reference. Unsupported formats (e.g.
    HEIC) are logged and yield empty results instead of failing the record."""
    image_ref = {"S3Object": {"Bucket": RAW_BUCKET, "Name": raw_key}}
    try:
        labels_resp = _rekognition.detect_labels(
            Image=image_ref, MaxLabels=MAX_LABELS, MinConfidence=MIN_LABEL_CONFIDENCE
        )
        faces_resp = _rekognition.detect_faces(Image=image_ref, Attributes=["ALL"])
    except ClientError as exc:
        warning(
            "rekognition skipped",
            file_id=file_id,
            error=exc.response.get("Error", {}).get("Code", str(exc)),
        )
        return [], []
    return extract_labels(labels_resp), extract_faces(faces_resp)


# --- Parquet ------------------------------------------------------------------

def build_parquet_bytes(record: dict[str, Any]) -> bytes:
    table = pa.Table.from_pylist([record], schema=PARQUET_SCHEMA)
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="snappy")
    return buf.getvalue()


def partition_prefix(year: int, month: int, day: int) -> str:
    return f"year={year:04d}/month={month:02d}/day={day:02d}"


# --- Catalog update -----------------------------------------------------------

def _update_catalog(file_id: str, fields: dict[str, Any]) -> None:
    names, values, sets = {}, {}, []
    for i, (key, value) in enumerate(fields.items()):
        names[f"#k{i}"] = key
        values[f":v{i}"] = value
        sets.append(f"#k{i} = :v{i}")
    _table.update_item(
        Key={"file_id": file_id},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


# --- Core processing ----------------------------------------------------------

def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def process_item(item: dict[str, Any]) -> dict[str, Any]:
    """Enrich a single catalog item (already deserialized from the stream)."""
    file_id = item["file_id"]
    raw_key = item["s3_raw_key"]
    year, month, day = int(item["year"]), int(item["month"]), int(item["day"])
    media_type = item.get("media_type", "unknown")
    info("enriching", file_id=file_id, media_type=media_type, s3_key=raw_key)

    exif: dict[str, Any] = {"capture_ts": None, "gps_lat": None, "gps_lon": None}
    thumbnail_key = None
    labels: list[str] = []
    faces: list[dict[str, Any]] = []

    if media_type == "photo":
        body = _s3.get_object(Bucket=RAW_BUCKET, Key=raw_key)["Body"].read()
        exif = extract_exif(body)
        try:
            thumb = make_thumbnail(body, THUMBNAIL_MAX_PX)
            thumbnail_key = f"thumbnails/{partition_prefix(year, month, day)}/{file_id}.jpg"
            _s3.put_object(
                Bucket=PROCESSED_BUCKET,
                Key=thumbnail_key,
                Body=thumb,
                ContentType="image/jpeg",
            )
        except Exception as exc:
            warning("thumbnail generation failed", file_id=file_id, error=str(exc))
            thumbnail_key = None
        labels, faces = run_rekognition(raw_key, file_id)
    # Videos: Rekognition's video APIs are async (Start*/Get* + SNS) and
    # billed per minute -- deferred. Metadata parquet is still written.

    record = {
        "file_id": file_id,
        "original_filename": item.get("original_filename"),
        "uploader": item.get("uploader"),
        "upload_ts": _parse_iso(item.get("upload_ts")),
        "capture_ts": exif["capture_ts"],
        "gps_lat": exif["gps_lat"],
        "gps_lon": exif["gps_lon"],
        # Reverse geocoding needs an external API (Amazon Location / Nominatim);
        # deliberately deferred -- column stays null for now.
        "location_name": None,
        "rekognition_labels": labels,
        "rekognition_faces": faces,
        "file_size_bytes": int(item.get("file_size_bytes", 0)),
        "media_type": media_type,
        "s3_raw_key": raw_key,
        "s3_thumbnail_key": thumbnail_key,
        "album_ids": [],
    }

    metadata_key = f"metadata/{partition_prefix(year, month, day)}/{file_id}.parquet"
    _s3.put_object(
        Bucket=PROCESSED_BUCKET,
        Key=metadata_key,
        Body=build_parquet_bytes(record),
        ContentType="application/octet-stream",
    )

    # Denormalized highlights on the catalog item; the parquet file remains
    # the analytical source of truth (faces detail lives only there).
    updates: dict[str, Any] = {
        "status": "enriched",
        "enriched_ts": datetime.now(timezone.utc).isoformat(),
        "s3_metadata_key": metadata_key,
        "rekognition_labels": labels,
        "face_count": len(faces),
    }
    if thumbnail_key:
        updates["s3_thumbnail_key"] = thumbnail_key
    if exif["capture_ts"]:
        updates["capture_ts"] = exif["capture_ts"].isoformat()
    if exif["gps_lat"] is not None:
        updates["gps_lat"] = Decimal(str(exif["gps_lat"]))
        updates["gps_lon"] = Decimal(str(exif["gps_lon"]))
    _update_catalog(file_id, updates)

    info(
        "enriched",
        file_id=file_id,
        labels=len(labels),
        faces=len(faces),
        s3_metadata_key=metadata_key,
        s3_thumbnail_key=thumbnail_key,
    )
    return record


# --- Handler --------------------------------------------------------------------

def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    # --- validate -----------------------------------------------------------
    records = (event or {}).get("Records") or []
    if not records:
        warning("event contained no stream records")
        return {"batchItemFailures": []}

    failures: list[dict[str, str]] = []

    # --- business logic -----------------------------------------------------
    for record in records:
        sequence = record.get("dynamodb", {}).get("SequenceNumber", "")
        try:
            if record.get("eventName") != "INSERT":
                continue
            new_image = record.get("dynamodb", {}).get("NewImage")
            if not new_image:
                continue
            item = {k: _deserializer.deserialize(v) for k, v in new_image.items()}
            if item.get("status") != "uploaded":
                info("skipping item not in 'uploaded' state", file_id=item.get("file_id"))
                continue
            process_item(item)
        except Exception as exc:  # noqa: BLE001 - report, let the stream retry
            error("record failed", sequence=sequence, error=str(exc))
            failures.append({"itemIdentifier": sequence})

    # --- response -----------------------------------------------------------
    info("batch complete", records=len(records), failed=len(failures))
    return {"batchItemFailures": failures}
