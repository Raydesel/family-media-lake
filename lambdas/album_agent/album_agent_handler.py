"""album_agent Lambda (Phase 3).

One Lambda, two actions, orchestrated by the nightly Step Functions workflow:

  {"action": "cluster_faces"}
      Index new enriched photos into a Rekognition face collection and group
      faces into person clusters (SearchFaces by face id). Cluster membership
      is persisted in the faces DynamoDB table and denormalized onto catalog
      items as face_cluster_ids.

  {"action": "propose_albums"}
      Collect enriched-but-unalbumed media from the catalog, compact it into
      a metadata JSON, and ask Claude (via Bedrock Converse) to propose
      albums. Valid proposals are written as:
        - albums/<album_id>/manifest.json          (processed bucket)
        - album_assignments/<run_id>.parquet       (Glue: album_assignments)
        - album_ids appended on catalog items      (DynamoDB)
      The returned summary feeds the SNS approval email sent by the workflow.

Handler pattern: validate event -> log -> business logic -> return response.
"""
from __future__ import annotations

import io
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

import boto3
import pyarrow as pa
import pyarrow.parquet as pq
from botocore.exceptions import ClientError

# --- Configuration from environment (set by Terraform) ----------------------
CATALOG_TABLE = os.environ["CATALOG_TABLE"]
FACES_TABLE = os.environ["FACES_TABLE"]
RAW_BUCKET = os.environ["RAW_BUCKET"]
PROCESSED_BUCKET = os.environ["PROCESSED_BUCKET"]
FACE_COLLECTION_ID = os.environ["FACE_COLLECTION_ID"]
BEDROCK_MODEL_ID = os.environ["BEDROCK_MODEL_ID"]
FACE_MATCH_THRESHOLD = float(os.environ.get("FACE_MATCH_THRESHOLD", "90"))
MAX_ITEMS_PER_RUN = int(os.environ.get("MAX_ITEMS_PER_RUN", "300"))
MIN_ITEMS_FOR_PROPOSAL = int(os.environ.get("MIN_ITEMS_FOR_PROPOSAL", "5"))
MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "2000"))

_dynamodb = boto3.resource("dynamodb")
_catalog = _dynamodb.Table(CATALOG_TABLE)
_faces = _dynamodb.Table(FACES_TABLE)
_s3 = boto3.client("s3")
_rekognition = boto3.client("rekognition")
_bedrock = boto3.client("bedrock-runtime")


# --- Structured logging ------------------------------------------------------

def _log(level: str, message: str, **fields) -> None:
    record = {"level": level, "function": "album_agent", "message": message}
    record.update({k: v for k, v in fields.items() if v is not None})
    stream = sys.stderr if level in ("ERROR", "WARNING") else sys.stdout
    print(json.dumps(record, default=str), file=stream)


def info(message: str, **fields) -> None:
    _log("INFO", message, **fields)


def warning(message: str, **fields) -> None:
    _log("WARNING", message, **fields)


def error(message: str, **fields) -> None:
    _log("ERROR", message, **fields)


# ===========================================================================
# Action: cluster_faces
# ===========================================================================

def _scan_catalog(filter_expression: str, expression_values: dict[str, Any],
                  expression_names: dict[str, str] | None = None,
                  limit: int = MAX_ITEMS_PER_RUN) -> list[dict[str, Any]]:
    """Paginated catalog scan. A scan is fine at family scale (thousands of
    items); revisit with a status GSI if the catalog ever grows huge."""
    items: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {
        "FilterExpression": filter_expression,
        "ExpressionAttributeValues": expression_values,
    }
    if expression_names:
        kwargs["ExpressionAttributeNames"] = expression_names
    while True:
        resp = _catalog.scan(**kwargs)
        items.extend(resp.get("Items", []))
        if len(items) >= limit or "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return items[:limit]


def resolve_cluster(match_face_ids: list[str], known_faces: dict[str, str]) -> str:
    """Pick the cluster of the first matched face we already know about;
    otherwise mint a new cluster id."""
    for face_id in match_face_ids:
        if face_id in known_faces:
            return known_faces[face_id]
    return str(uuid.uuid4())


def _index_photo_faces(item: dict[str, Any]) -> list[dict[str, str]]:
    """IndexFaces + SearchFaces for one photo. Returns face->cluster rows."""
    file_id = item["file_id"]
    try:
        indexed = _rekognition.index_faces(
            CollectionId=FACE_COLLECTION_ID,
            Image={"S3Object": {"Bucket": RAW_BUCKET, "Name": item["s3_raw_key"]}},
            ExternalImageId=file_id,
            MaxFaces=10,
            QualityFilter="AUTO",
            DetectionAttributes=[],
        )
    except ClientError as exc:
        warning(
            "index_faces skipped",
            file_id=file_id,
            error=exc.response.get("Error", {}).get("Code", str(exc)),
        )
        return []

    rows = []
    for record in indexed.get("FaceRecords", []):
        face_id = record["Face"]["FaceId"]
        try:
            matches = _rekognition.search_faces(
                CollectionId=FACE_COLLECTION_ID,
                FaceId=face_id,
                FaceMatchThreshold=FACE_MATCH_THRESHOLD,
                MaxFaces=10,
            ).get("FaceMatches", [])
        except ClientError as exc:
            warning("search_faces failed", face_id=face_id, error=str(exc))
            matches = []

        match_ids = [m["Face"]["FaceId"] for m in matches]
        known = _lookup_known_faces(match_ids)
        cluster_id = resolve_cluster(match_ids, known)
        rows.append({"face_id": face_id, "file_id": file_id, "cluster_id": cluster_id})
    return rows


def _lookup_known_faces(face_ids: list[str]) -> dict[str, str]:
    """face_id -> cluster_id for the ids we have already persisted."""
    known: dict[str, str] = {}
    for face_id in face_ids:
        resp = _faces.get_item(Key={"face_id": face_id})
        if "Item" in resp and "cluster_id" in resp["Item"]:
            known[face_id] = resp["Item"]["cluster_id"]
    return known


def cluster_faces() -> dict[str, Any]:
    candidates = _scan_catalog(
        filter_expression=(
            "#st = :enriched AND face_count > :zero AND attribute_not_exists(faces_indexed)"
        ),
        expression_values={":enriched": "enriched", ":zero": 0},
        expression_names={"#st": "status"},
    )
    info("face clustering candidates", count=len(candidates))

    now = datetime.now(timezone.utc).isoformat()
    photos_indexed = 0
    faces_indexed = 0
    clusters: set[str] = set()

    for item in candidates:
        rows = _index_photo_faces(item)
        cluster_ids = sorted({r["cluster_id"] for r in rows})
        for row in rows:
            _faces.put_item(Item={**row, "indexed_ts": now})
        _catalog.update_item(
            Key={"file_id": item["file_id"]},
            UpdateExpression="SET faces_indexed = :t, face_cluster_ids = :c",
            ExpressionAttributeValues={":t": True, ":c": cluster_ids},
        )
        photos_indexed += 1
        faces_indexed += len(rows)
        clusters.update(cluster_ids)

    result = {
        "photos_indexed": photos_indexed,
        "faces_indexed": faces_indexed,
        "clusters_touched": len(clusters),
    }
    info("face clustering complete", **result)
    return result


# ===========================================================================
# Action: propose_albums
# ===========================================================================

SYSTEM_PROMPT = """\
You are an archivist organizing a family photo library. You receive a JSON
list of media items (id, date, uploader, labels, location, face clusters) and
group them into meaningful albums: trips, events, holidays, recurring people.

Rules:
- Only propose albums with at least 3 items.
- Every file_id you use MUST come from the input list.
- An item may appear in at most 2 albums.
- Prefer fewer, well-defined albums over many vague ones.
- Album names: short, warm, specific (e.g. "Beach Week, June 2026").

Respond with ONLY this JSON, no prose:
{"albums": [{"name": "...", "description": "...", "file_ids": ["..."]}]}
"""


def compact_item(item: dict[str, Any]) -> dict[str, Any]:
    """Reduce a catalog item to the few fields Claude needs (token budget)."""
    labels = [str(l) for l in (item.get("rekognition_labels") or [])][:8]
    clusters = [str(c)[:8] for c in (item.get("face_cluster_ids") or [])]
    out: dict[str, Any] = {
        "file_id": str(item["file_id"]),
        "date": str(item.get("capture_ts") or item.get("upload_ts") or "")[:10],
        "uploader": str(item.get("uploader", "unknown")),
        "media_type": str(item.get("media_type", "photo")),
        "labels": labels,
    }
    if clusters:
        out["people"] = clusters
    if item.get("gps_lat") is not None and item.get("gps_lon") is not None:
        out["gps"] = [round(float(item["gps_lat"]), 3), round(float(item["gps_lon"]), 3)]
    return out


def parse_albums_json(text: str) -> list[dict[str, Any]]:
    """Extract the albums list from a model response, tolerating code fences
    and stray prose around the JSON object."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in model response")
    payload = json.loads(text[start : end + 1])
    albums = payload.get("albums")
    if not isinstance(albums, list):
        raise ValueError("model response missing 'albums' list")
    return albums


def validate_proposals(albums: list[dict[str, Any]], valid_file_ids: set[str]) -> list[dict[str, Any]]:
    """Drop hallucinated file ids, empty names, and too-small albums."""
    cleaned = []
    for album in albums:
        name = str(album.get("name") or "").strip()
        file_ids = [f for f in (album.get("file_ids") or []) if f in valid_file_ids]
        if not name or len(file_ids) < 3:
            continue
        cleaned.append(
            {
                "name": name[:120],
                "description": str(album.get("description") or "").strip()[:500],
                "file_ids": sorted(set(file_ids)),
            }
        )
    return cleaned


def invoke_claude(items: list[dict[str, Any]]) -> str:
    user_payload = (
        "Group these family media items into albums. Today is "
        f"{datetime.now(timezone.utc).date().isoformat()}.\n\n"
        + json.dumps(items, separators=(",", ":"))
    )
    response = _bedrock.converse(
        modelId=BEDROCK_MODEL_ID,
        system=[{"text": SYSTEM_PROMPT}],
        messages=[{"role": "user", "content": [{"text": user_payload}]}],
        inferenceConfig={"maxTokens": MAX_OUTPUT_TOKENS, "temperature": 0.2},
    )
    usage = response.get("usage", {})
    info(
        "bedrock call complete",
        model_id=BEDROCK_MODEL_ID,
        input_tokens=usage.get("inputTokens"),
        output_tokens=usage.get("outputTokens"),
    )
    return response["output"]["message"]["content"][0]["text"]


ASSIGNMENTS_SCHEMA = pa.schema(
    [
        ("album_id", pa.string()),
        ("album_name", pa.string()),
        ("file_id", pa.string()),
        ("status", pa.string()),
        ("assigned_ts", pa.timestamp("ms", tz="UTC")),
        ("model_id", pa.string()),
    ]
)


def build_assignments_parquet(rows: list[dict[str, Any]]) -> bytes:
    table = pa.Table.from_pylist(rows, schema=ASSIGNMENTS_SCHEMA)
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="snappy")
    return buf.getvalue()


def _persist_album(album: dict[str, Any], run_ts: datetime) -> tuple[str, list[dict[str, Any]]]:
    """Write the manifest and return (album_id, assignment rows)."""
    album_id = str(uuid.uuid4())
    manifest = {
        "album_id": album_id,
        "name": album["name"],
        "description": album["description"],
        "file_ids": album["file_ids"],
        "status": "proposed",
        "created_ts": run_ts.isoformat(),
        "model_id": BEDROCK_MODEL_ID,
    }
    _s3.put_object(
        Bucket=PROCESSED_BUCKET,
        Key=f"albums/{album_id}/manifest.json",
        Body=json.dumps(manifest, indent=2).encode(),
        ContentType="application/json",
    )
    rows = [
        {
            "album_id": album_id,
            "album_name": album["name"],
            "file_id": file_id,
            "status": "proposed",
            "assigned_ts": run_ts,
            "model_id": BEDROCK_MODEL_ID,
        }
        for file_id in album["file_ids"]
    ]
    return album_id, rows


def propose_albums() -> dict[str, Any]:
    candidates = _scan_catalog(
        filter_expression="#st = :enriched AND attribute_not_exists(album_ids)",
        expression_values={":enriched": "enriched"},
        expression_names={"#st": "status"},
    )
    info("album candidates", count=len(candidates))

    if len(candidates) < MIN_ITEMS_FOR_PROPOSAL:
        info("not enough unalbumed media; skipping proposal run")
        return {"albums": 0, "assigned": 0, "summary": "", "skipped": True}

    compact = [compact_item(i) for i in candidates]
    raw_response = invoke_claude(compact)

    try:
        proposals = parse_albums_json(raw_response)
    except (ValueError, json.JSONDecodeError) as exc:
        # Bad model output: fail the run loudly -> Step Functions retries.
        error("unparseable model response", error=str(exc), response=raw_response[:500])
        raise

    albums = validate_proposals(proposals, {c["file_id"] for c in compact})
    if not albums:
        info("model proposed no valid albums")
        return {"albums": 0, "assigned": 0, "summary": "", "skipped": False}

    run_ts = datetime.now(timezone.utc)
    run_id = run_ts.strftime("%Y%m%dT%H%M%SZ") + "_" + str(uuid.uuid4())[:8]
    all_rows: list[dict[str, Any]] = []
    file_to_albums: dict[str, list[str]] = {}

    for album in albums:
        album_id, rows = _persist_album(album, run_ts)
        all_rows.extend(rows)
        for file_id in album["file_ids"]:
            file_to_albums.setdefault(file_id, []).append(album_id)

    assignments_key = f"album_assignments/{run_id}.parquet"
    _s3.put_object(
        Bucket=PROCESSED_BUCKET,
        Key=assignments_key,
        Body=build_assignments_parquet(all_rows),
        ContentType="application/octet-stream",
    )

    for file_id, album_ids in file_to_albums.items():
        _catalog.update_item(
            Key={"file_id": file_id},
            UpdateExpression=(
                "SET album_ids = list_append(if_not_exists(album_ids, :empty), :new)"
            ),
            ExpressionAttributeValues={":empty": [], ":new": album_ids},
        )

    lines = [f"- {a['name']} ({len(a['file_ids'])} items): {a['description']}" for a in albums]
    summary = (
        f"The nightly media agent proposed {len(albums)} album(s) "
        f"from {len(candidates)} new items:\n\n" + "\n".join(lines) + "\n\n"
        f"Manifests: s3://{PROCESSED_BUCKET}/albums/ (status=proposed)\n"
        f"Assignments: s3://{PROCESSED_BUCKET}/{assignments_key}"
    )

    result = {
        "albums": len(albums),
        "assigned": len(all_rows),
        "album_names": [a["name"] for a in albums],
        "assignments_key": assignments_key,
        "summary": summary,
        "skipped": False,
    }
    info("album proposal complete", albums=len(albums), assigned=len(all_rows))
    return result


# ===========================================================================
# Handler
# ===========================================================================

ACTIONS = {
    "cluster_faces": cluster_faces,
    "propose_albums": propose_albums,
}


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    # --- validate -----------------------------------------------------------
    action = (event or {}).get("action")
    if action not in ACTIONS:
        error("unknown action", action=action, expected=sorted(ACTIONS))
        raise ValueError(f"unknown action: {action!r}")

    # --- business logic + response --------------------------------------------
    info("action start", action=action)
    return ACTIONS[action]()
