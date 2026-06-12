"""search_api Lambda (Phase 4).

HTTP API (API Gateway v2) handler backed by Athena over the media_metadata
Glue table. Cognito JWT validation is done by API Gateway before this Lambda
runs; we only read claims for logging.

Routes:
  GET /health          — unauthenticated liveness probe
  GET /search          — query media_metadata (requires Cognito JWT)

Query parameters (all optional except partition hints strongly recommended):
  year, month, day     — partition predicates (year defaults to UTC now)
  label                — Rekognition label (contains)
  uploader             — exact uploader name
  media_type           — photo | video
  limit                — max rows (default 50, cap 100)

Handler pattern: validate event -> log -> business logic -> return response.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any

import boto3

# --- Configuration from environment (set by Terraform) ----------------------
GLUE_DATABASE = os.environ["GLUE_DATABASE"]
GLUE_TABLE = os.environ["GLUE_TABLE"]
ATHENA_WORKGROUP = os.environ["ATHENA_WORKGROUP"]
CLOUDFRONT_DOMAIN = os.environ["CLOUDFRONT_DOMAIN"].rstrip("/")
QUERY_TIMEOUT_SEC = int(os.environ.get("QUERY_TIMEOUT_SEC", "25"))
DEFAULT_LIMIT = int(os.environ.get("DEFAULT_LIMIT", "50"))
MAX_LIMIT = int(os.environ.get("MAX_LIMIT", "100"))

_athena = boto3.client("athena")

_UPLOADER_RE = re.compile(r"^[a-zA-Z0-9._ -]{1,64}$")
_LABEL_RE = re.compile(r"^[a-zA-Z0-9 ,._'-]{1,80}$")
_MEDIA_TYPES = {"photo", "video"}


# --- Structured logging ------------------------------------------------------

def _log(level: str, message: str, **fields) -> None:
    record = {"level": level, "function": "search_api", "message": message}
    record.update({k: v for k, v in fields.items() if v is not None})
    stream = sys.stderr if level in ("ERROR", "WARNING") else sys.stdout
    print(json.dumps(record, default=str), file=stream)


def info(message: str, **fields) -> None:
    _log("INFO", message, **fields)


def warning(message: str, **fields) -> None:
    _log("WARNING", message, **fields)


def error(message: str, **fields) -> None:
    _log("ERROR", message, **fields)


# --- HTTP helpers ------------------------------------------------------------

def api_response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body, default=str),
    }


def _caller_sub(event: dict[str, Any]) -> str | None:
    try:
        return event["requestContext"]["authorizer"]["jwt"]["claims"]["sub"]
    except (KeyError, TypeError):
        return None


# --- Query building (pure, unit-testable) ------------------------------------

def _parse_int(name: str, raw: str | None, lo: int, hi: int) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not lo <= value <= hi:
        raise ValueError(f"{name} must be between {lo} and {hi}")
    return value


def build_search_query(params: dict[str, str | None]) -> tuple[str, int]:
    """Build a partition-pruned Athena SQL string from query parameters."""
    now = datetime.now(timezone.utc)
    year = _parse_int("year", params.get("year"), 2000, 2035) or now.year
    month = _parse_int("month", params.get("month"), 1, 12)
    day = _parse_int("day", params.get("day"), 1, 31)

    limit = _parse_int("limit", params.get("limit"), 1, MAX_LIMIT) or DEFAULT_LIMIT

    uploader = (params.get("uploader") or "").strip()
    if uploader and not _UPLOADER_RE.match(uploader):
        raise ValueError("uploader contains invalid characters")

    label = (params.get("label") or params.get("q") or "").strip()
    if label and not _LABEL_RE.match(label):
        raise ValueError("label contains invalid characters")

    media_type = (params.get("media_type") or "").strip().lower()
    if media_type and media_type not in _MEDIA_TYPES:
        raise ValueError("media_type must be photo or video")

    # Identifier quoting: Glue table/database names come from Terraform env.
    table = f'"{GLUE_DATABASE}"."{GLUE_TABLE}"'
    clauses = [f"year = {year}"]
    if month is not None:
        clauses.append(f"month = {month}")
    if day is not None:
        clauses.append(f"day = {day}")
    if uploader:
        clauses.append(f"uploader = '{uploader.replace(chr(39), chr(39)+chr(39))}'")
    if label:
        safe_label = label.replace("'", "''")
        clauses.append(f"contains(rekognition_labels, '{safe_label}')")
    if media_type:
        clauses.append(f"media_type = '{media_type}'")

    sql = (
        "SELECT file_id, original_filename, uploader, upload_ts, capture_ts, "
        "media_type, rekognition_labels, s3_thumbnail_key "
        f"FROM {table} WHERE {' AND '.join(clauses)} "
        "ORDER BY capture_ts DESC NULLS LAST, upload_ts DESC "
        f"LIMIT {limit}"
    )
    return sql, limit


# --- Athena execution ----------------------------------------------------------

def _rows_from_results(result: dict[str, Any]) -> list[dict[str, Any]]:
    rows = result.get("ResultSet", {}).get("Rows", [])
    if len(rows) < 2:
        return []
    headers = [c.get("VarCharValue", "") for c in rows[0]["Data"]]
    out = []
    for row in rows[1:]:
        values = [c.get("VarCharValue") for c in row["Data"]]
        out.append(dict(zip(headers, values)))
    return out


def thumbnail_url(s3_key: str | None) -> str | None:
    if not s3_key:
        return None
    key = s3_key.lstrip("/")
    return f"https://{CLOUDFRONT_DOMAIN}/{key}"


def _parse_label_array(raw: str | None) -> list[str]:
    """Athena returns array columns as bracketed strings."""
    if not raw:
        return []
    text = raw.strip()
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [part.strip().strip('"') for part in inner.split(",") if part.strip()]
    return [text]


def enrich_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched = []
    for row in rows:
        item = dict(row)
        item["thumbnail_url"] = thumbnail_url(item.get("s3_thumbnail_key"))
        if isinstance(item.get("rekognition_labels"), str):
            item["rekognition_labels"] = _parse_label_array(item["rekognition_labels"])
        enriched.append(item)
    return enriched


def run_athena_query(sql: str) -> list[dict[str, Any]]:
    """Start an Athena query and poll until complete or timeout."""
    execution = _athena.start_query_execution(
        QueryString=sql,
        QueryExecutionContext={"Database": GLUE_DATABASE},
        WorkGroup=ATHENA_WORKGROUP,
    )
    qid = execution["QueryExecutionId"]
    deadline = time.time() + QUERY_TIMEOUT_SEC

    while time.time() < deadline:
        status = _athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]
        state = status["State"]
        if state == "SUCCEEDED":
            result = _athena.get_query_results(QueryExecutionId=qid, MaxResults=1000)
            return _rows_from_results(result)
        if state in ("FAILED", "CANCELLED"):
            reason = status.get("StateChangeReason", state)
            raise RuntimeError(f"athena query {state}: {reason}")
        time.sleep(0.4)

    _athena.stop_query_execution(QueryExecutionId=qid)
    raise TimeoutError(f"athena query timed out after {QUERY_TIMEOUT_SEC}s")


def search(params: dict[str, str | None]) -> dict[str, Any]:
    sql, limit = build_search_query(params)
    rows = enrich_rows(run_athena_query(sql))
    return {"count": len(rows), "limit": limit, "results": rows}


# --- Handler -----------------------------------------------------------------

def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    # --- validate routing -----------------------------------------------------
    path = (event or {}).get("rawPath") or (event or {}).get("path") or "/"
    method = (
        (event or {}).get("requestContext", {}).get("http", {}).get("method")
        or (event or {}).get("httpMethod")
        or "GET"
    )

    if path == "/health" and method == "GET":
        return api_response(200, {"status": "ok", "service": "search_api"})

    if path != "/search" or method != "GET":
        return api_response(404, {"error": "not_found", "path": path})

    params = (event or {}).get("queryStringParameters") or {}
    caller = _caller_sub(event)
    info("search request", caller_sub=caller, params=params)

    # --- business logic -------------------------------------------------------
    try:
        payload = search(params)
    except ValueError as exc:
        warning("invalid query parameters", error=str(exc))
        return api_response(400, {"error": "bad_request", "message": str(exc)})
    except TimeoutError as exc:
        error("query timeout", error=str(exc))
        return api_response(504, {"error": "timeout", "message": str(exc)})
    except Exception as exc:  # noqa: BLE001
        error("search failed", error=str(exc))
        return api_response(500, {"error": "internal_error", "message": "search failed"})

    info("search complete", count=payload["count"])
    return api_response(200, payload)
