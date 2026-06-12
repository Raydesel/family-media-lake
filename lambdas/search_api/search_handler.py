"""search_api Lambda (Phase 4).

HTTP API (API Gateway v2) handler backed by Athena over the media_metadata
Glue table. Cognito JWT validation is done by API Gateway before this Lambda
runs; we only read claims for logging.

Routes:
  GET /health          — unauthenticated liveness probe
  GET /search          — browse/search with pagination (requires Cognito JWT)
  GET /years           — distinct years with media (requires Cognito JWT)
  GET /download        — presigned URL for the original file (requires Cognito JWT)

Search query parameters:
  year                 — YYYY, or "all" (default) for every projected year
  month, day           — optional partition predicates
  label / q            — Rekognition label (contains)
  uploader             — exact uploader name
  media_type           — photo | video
  page                 — 1-based page number (default 1)
  page_size            — rows per page (default 20, max 20)

Handler pattern: validate event -> log -> business logic -> return response.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from urllib.parse import quote
from typing import Any

import boto3

# --- Configuration from environment (set by Terraform) ----------------------
GLUE_DATABASE = os.environ["GLUE_DATABASE"]
GLUE_TABLE = os.environ["GLUE_TABLE"]
ATHENA_WORKGROUP = os.environ["ATHENA_WORKGROUP"]
CLOUDFRONT_DOMAIN = os.environ["CLOUDFRONT_DOMAIN"].rstrip("/")
RAW_BUCKET = os.environ["RAW_BUCKET"]
CATALOG_TABLE = os.environ["CATALOG_TABLE"]
# dynamodb = millisecond browse/download for family-scale catalogs.
# athena = better for huge lakes or ad-hoc SQL (set BROWSE_BACKEND=athena).
BROWSE_BACKEND = os.environ.get("BROWSE_BACKEND", "dynamodb").lower()
QUERY_TIMEOUT_SEC = int(os.environ.get("QUERY_TIMEOUT_SEC", "25"))
DEFAULT_PAGE_SIZE = int(os.environ.get("DEFAULT_PAGE_SIZE", "20"))
MAX_PAGE_SIZE = int(os.environ.get("MAX_PAGE_SIZE", "20"))
YEAR_MIN = int(os.environ.get("YEAR_MIN", "2000"))
YEAR_MAX = int(os.environ.get("YEAR_MAX", "2035"))
DOWNLOAD_URL_TTL = int(os.environ.get("DOWNLOAD_URL_TTL", "3600"))
CATALOG_CACHE_TTL = int(os.environ.get("CATALOG_CACHE_TTL", "30"))

_athena = boto3.client("athena")
_s3 = boto3.client("s3")
_catalog = boto3.resource("dynamodb").Table(CATALOG_TABLE)
# Warm-Lambda cache so /years + /search in the same window share one scan.
_catalog_cache: tuple[float, list[dict[str, Any]]] | None = None

_UPLOADER_RE = re.compile(r"^[a-zA-Z0-9._ -]{1,64}$")
_LABEL_RE = re.compile(r"^[a-zA-Z0-9 ,._'-]{1,80}$")
_MEDIA_TYPES = {"photo", "video"}
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

_SELECT_COLUMNS = (
    "file_id, original_filename, uploader, upload_ts, capture_ts, "
    "media_type, rekognition_labels, s3_thumbnail_key, s3_raw_key"
)


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


def _glue_table() -> str:
    return f'"{GLUE_DATABASE}"."{GLUE_TABLE}"'


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


def _year_clause(params: dict[str, str | None]) -> str:
    year_raw = (params.get("year") or "all").strip().lower()
    if year_raw in ("all", "*", ""):
        return f"year BETWEEN {YEAR_MIN} AND {YEAR_MAX}"
    year = _parse_int("year", params.get("year"), YEAR_MIN, YEAR_MAX)
    if year is None:
        return f"year BETWEEN {YEAR_MIN} AND {YEAR_MAX}"
    return f"year = {year}"


def _filter_clauses(params: dict[str, str | None]) -> list[str]:
    clauses = [_year_clause(params)]

    month = _parse_int("month", params.get("month"), 1, 12)
    day = _parse_int("day", params.get("day"), 1, 31)
    if month is not None:
        clauses.append(f"month = {month}")
    if day is not None:
        clauses.append(f"day = {day}")

    uploader = (params.get("uploader") or "").strip()
    if uploader:
        if not _UPLOADER_RE.match(uploader):
            raise ValueError("uploader contains invalid characters")
        clauses.append(f"uploader = '{uploader.replace(chr(39), chr(39) + chr(39))}'")

    label = (params.get("label") or params.get("q") or "").strip()
    if label:
        if not _LABEL_RE.match(label):
            raise ValueError("label contains invalid characters")
        clauses.append(f"contains(rekognition_labels, '{label.replace(chr(39), chr(39) + chr(39))}')")

    media_type = (params.get("media_type") or "").strip().lower()
    if media_type:
        if media_type not in _MEDIA_TYPES:
            raise ValueError("media_type must be photo or video")
        clauses.append(f"media_type = '{media_type}'")

    return clauses


def build_search_query(params: dict[str, str | None]) -> tuple[str, int, int]:
    """Build paginated browse/search SQL. Fetches page_size+1 rows for has_more."""
    page = _parse_int("page", params.get("page"), 1, 10_000) or 1
    page_size = _parse_int("page_size", params.get("page_size"), 1, MAX_PAGE_SIZE) or DEFAULT_PAGE_SIZE
    offset = (page - 1) * page_size
    fetch_limit = page_size + 1

    where = " AND ".join(_filter_clauses(params))
    sql = (
        f"SELECT {_SELECT_COLUMNS} FROM {_glue_table()} WHERE {where} "
        "ORDER BY year DESC, month DESC, day DESC, "
        "capture_ts DESC NULLS LAST, upload_ts DESC "
        f"OFFSET {offset} LIMIT {fetch_limit}"
    )
    return sql, page, page_size


def build_years_query() -> str:
    return (
        f"SELECT DISTINCT year FROM {_glue_table()} "
        f"WHERE year BETWEEN {YEAR_MIN} AND {YEAR_MAX} "
        "ORDER BY year DESC"
    )


def build_file_lookup_query(file_id: str) -> str:
    safe_id = file_id.lower()
    return (
        f"SELECT s3_raw_key, original_filename, media_type FROM {_glue_table()} "
        f"WHERE file_id = '{safe_id}' LIMIT 1"
    )


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


# --- DynamoDB fast path (family-scale browse) --------------------------------

def _from_dynamo(value: Any) -> Any:
    from decimal import Decimal

    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    if isinstance(value, list):
        return [_from_dynamo(v) for v in value]
    if isinstance(value, dict):
        return {k: _from_dynamo(v) for k, v in value.items()}
    return value


def _catalog_to_row(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "file_id": item.get("file_id"),
        "original_filename": item.get("original_filename"),
        "uploader": item.get("uploader"),
        "upload_ts": item.get("upload_ts"),
        "capture_ts": item.get("capture_ts"),
        "media_type": item.get("media_type"),
        "rekognition_labels": item.get("rekognition_labels") or [],
        "s3_thumbnail_key": item.get("s3_thumbnail_key"),
        "s3_raw_key": item.get("s3_raw_key"),
    }


def _catalog_sort_key(item: dict[str, Any]) -> str:
    return str(item.get("capture_ts") or item.get("upload_ts") or "")


def _catalog_matches(item: dict[str, Any], params: dict[str, str | None]) -> bool:
    year_raw = (params.get("year") or "all").strip().lower()
    if year_raw not in ("all", "*", ""):
        if int(item.get("year", -1)) != int(year_raw):
            return False

    month = _parse_int("month", params.get("month"), 1, 12)
    if month is not None and int(item.get("month", -1)) != month:
        return False

    day = _parse_int("day", params.get("day"), 1, 31)
    if day is not None and int(item.get("day", -1)) != day:
        return False

    uploader = (params.get("uploader") or "").strip()
    if uploader and item.get("uploader") != uploader:
        return False

    label = (params.get("label") or params.get("q") or "").strip()
    if label:
        labels = item.get("rekognition_labels") or []
        if isinstance(labels, str):
            labels = _parse_label_array(labels)
        if label not in labels:
            return False

    media_type = (params.get("media_type") or "").strip().lower()
    if media_type and item.get("media_type") != media_type:
        return False

    return True


def _scan_enriched_catalog() -> list[dict[str, Any]]:
    global _catalog_cache
    now = time.time()
    if _catalog_cache and now - _catalog_cache[0] < CATALOG_CACHE_TTL:
        return _catalog_cache[1]

    items: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {
        "FilterExpression": "#st = :enriched",
        "ExpressionAttributeNames": {"#st": "status"},
        "ExpressionAttributeValues": {":enriched": "enriched"},
    }
    while True:
        resp = _catalog.scan(**kwargs)
        items.extend(_from_dynamo(i) for i in resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    _catalog_cache = (now, items)
    return items


def catalog_search(params: dict[str, str | None]) -> dict[str, Any]:
    page = _parse_int("page", params.get("page"), 1, 10_000) or 1
    page_size = _parse_int("page_size", params.get("page_size"), 1, MAX_PAGE_SIZE) or DEFAULT_PAGE_SIZE

    uploader = (params.get("uploader") or "").strip()
    if uploader and not _UPLOADER_RE.match(uploader):
        raise ValueError("uploader contains invalid characters")
    label = (params.get("label") or params.get("q") or "").strip()
    if label and not _LABEL_RE.match(label):
        raise ValueError("label contains invalid characters")
    media_type = (params.get("media_type") or "").strip().lower()
    if media_type and media_type not in _MEDIA_TYPES:
        raise ValueError("media_type must be photo or video")

    matched = [i for i in _scan_enriched_catalog() if _catalog_matches(i, params)]
    matched.sort(key=_catalog_sort_key, reverse=True)

    offset = (page - 1) * page_size
    slice_ = matched[offset : offset + page_size + 1]
    has_more = len(slice_) > page_size
    rows = enrich_rows([_catalog_to_row(i) for i in slice_[:page_size]])
    return {
        "page": page,
        "page_size": page_size,
        "count": len(rows),
        "has_more": has_more,
        "results": rows,
        "source": "dynamodb",
    }


def catalog_list_years() -> dict[str, Any]:
    years = sorted({int(i["year"]) for i in _scan_enriched_catalog() if i.get("year") is not None}, reverse=True)
    return {"years": years, "source": "dynamodb"}


def catalog_get_item(file_id: str) -> dict[str, Any]:
    resp = _catalog.get_item(Key={"file_id": file_id.lower()})
    item = resp.get("Item")
    if not item:
        raise LookupError("photo not found")
    return _from_dynamo(item)


def run_athena_query(sql: str) -> list[dict[str, Any]]:
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
    if BROWSE_BACKEND == "dynamodb":
        return catalog_search(params)

    sql, page, page_size = build_search_query(params)
    rows = enrich_rows(run_athena_query(sql))
    has_more = len(rows) > page_size
    if has_more:
        rows = rows[:page_size]
    return {
        "page": page,
        "page_size": page_size,
        "count": len(rows),
        "has_more": has_more,
        "results": rows,
        "source": "athena",
    }


def list_years() -> dict[str, Any]:
    if BROWSE_BACKEND == "dynamodb":
        return catalog_list_years()
    rows = run_athena_query(build_years_query())
    years = [int(r["year"]) for r in rows if r.get("year")]
    return {"years": years, "source": "athena"}


def presigned_download(file_id: str) -> dict[str, Any]:
    if not _UUID_RE.match(file_id):
        raise ValueError("file_id must be a UUID")

    if BROWSE_BACKEND == "dynamodb":
        row = catalog_get_item(file_id)
    else:
        rows = run_athena_query(build_file_lookup_query(file_id))
        if not rows:
            raise LookupError("photo not found")
        row = rows[0]

    raw_key = row.get("s3_raw_key")
    if not raw_key:
        raise LookupError("original file not available")

    filename = row.get("original_filename") or f"{file_id}.jpg"
    # attachment disposition prompts the browser to save instead of inline-preview.
    url = _s3.generate_presigned_url(
        ClientMethod="get_object",
        Params={
            "Bucket": RAW_BUCKET,
            "Key": raw_key,
            "ResponseContentDisposition": f'attachment; filename="{quote(filename)}"',
        },
        ExpiresIn=DOWNLOAD_URL_TTL,
    )
    return {
        "file_id": file_id,
        "filename": filename,
        "media_type": row.get("media_type"),
        "download_url": url,
        "expires_in": DOWNLOAD_URL_TTL,
    }


# --- Handler -----------------------------------------------------------------

def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    path = (event or {}).get("rawPath") or (event or {}).get("path") or "/"
    method = (
        (event or {}).get("requestContext", {}).get("http", {}).get("method")
        or (event or {}).get("httpMethod")
        or "GET"
    )
    params = (event or {}).get("queryStringParameters") or {}

    if path == "/health" and method == "GET":
        return api_response(200, {"status": "ok", "service": "search_api"})

    if method != "GET":
        return api_response(405, {"error": "method_not_allowed"})

    caller = _caller_sub(event)

    try:
        if path == "/search":
            info("search request", caller_sub=caller, params=params)
            payload = search(params)
            info("search complete", page=payload["page"], count=payload["count"])
            return api_response(200, payload)

        if path == "/years":
            info("years request", caller_sub=caller)
            return api_response(200, list_years())

        if path == "/download":
            file_id = (params.get("file_id") or "").strip()
            if not file_id:
                return api_response(400, {"error": "bad_request", "message": "file_id is required"})
            info("download request", caller_sub=caller, file_id=file_id)
            return api_response(200, presigned_download(file_id))

    except ValueError as exc:
        warning("invalid request", error=str(exc))
        return api_response(400, {"error": "bad_request", "message": str(exc)})
    except LookupError as exc:
        return api_response(404, {"error": "not_found", "message": str(exc)})
    except TimeoutError as exc:
        error("query timeout", error=str(exc))
        return api_response(504, {"error": "timeout", "message": str(exc)})
    except Exception as exc:  # noqa: BLE001
        error("request failed", path=path, error=str(exc))
        return api_response(500, {"error": "internal_error", "message": "request failed"})

    return api_response(404, {"error": "not_found", "path": path})
