"""Unit tests for the search_api Lambda."""
import json

import pytest

import search_handler as search


# --- build_search_query --------------------------------------------------------

def test_build_search_query_all_years_paginated():
    sql, page, page_size = search.build_search_query({"year": "all", "page": "2", "page_size": "20"})
    assert "year BETWEEN" in sql
    assert "OFFSET 20" in sql
    assert "LIMIT 21" in sql  # page_size + 1 for has_more
    assert page == 2 and page_size == 20


def test_build_search_query_single_year():
    sql, _, _ = search.build_search_query({"year": "2026"})
    assert "year = 2026" in sql
    assert "OFFSET 0" in sql


def test_build_search_query_full_filters():
    sql, page, page_size = search.build_search_query(
        {
            "year": "2026",
            "month": "6",
            "day": "10",
            "label": "Beach",
            "uploader": "family",
            "media_type": "photo",
            "page": "1",
            "page_size": "20",
        }
    )
    assert "year = 2026" in sql
    assert "month = 6" in sql
    assert "day = 10" in sql
    assert "contains(rekognition_labels, 'Beach')" in sql
    assert "uploader = 'family'" in sql
    assert "media_type = 'photo'" in sql
    assert page == 1 and page_size == 20


def test_build_search_query_accepts_q_alias():
    sql, _, _ = search.build_search_query({"year": "2026", "q": "Dog"})
    assert "contains(rekognition_labels, 'Dog')" in sql


def test_build_search_query_rejects_bad_uploader():
    with pytest.raises(ValueError, match="uploader"):
        search.build_search_query({"year": "2026", "uploader": "bad;drop"})


def test_build_search_query_escapes_quotes_in_label():
    sql, _, _ = search.build_search_query({"year": "2026", "label": "Kid's Party"})
    assert "Kid''s Party" in sql


def test_build_file_lookup_query():
    sql = search.build_file_lookup_query("3f2504e0-4f89-41d3-9a0c-0305e82c3301")
    assert "3f2504e0-4f89-41d3-9a0c-0305e82c3301" in sql


# --- enrich helpers ------------------------------------------------------------

def test_thumbnail_url():
    url = search.thumbnail_url("thumbnails/year=2026/month=06/day=10/x.jpg")
    assert url == "https://d111111abcdef8.cloudfront.net/thumbnails/year=2026/month=06/day=10/x.jpg"


def test_parse_label_array():
    assert search._parse_label_array("[Beach, Person]") == ["Beach", "Person"]
    assert search._parse_label_array("[]") == []


def test_enrich_rows_adds_thumbnail_and_download_url(monkeypatch):
    monkeypatch.setattr(
        search,
        "build_presigned_download_url",
        lambda raw_key, filename: f"https://signed.example/{raw_key}",
    )
    rows = search.enrich_rows(
        [
            {
                "s3_thumbnail_key": "thumbnails/year=2026/month=06/day=10/x.jpg",
                "s3_raw_key": "raw/year=2026/month=06/day=10/x.jpg",
                "original_filename": "beach.jpg",
            }
        ]
    )
    assert rows[0]["thumbnail_url"].endswith("thumbnails/year=2026/month=06/day=10/x.jpg")
    assert rows[0]["download_url"] == "https://signed.example/raw/year=2026/month=06/day=10/x.jpg"


def test_rows_from_results():
    result = {
        "ResultSet": {
            "Rows": [
                {"Data": [{"VarCharValue": "file_id"}, {"VarCharValue": "name"}]},
                {"Data": [{"VarCharValue": "abc"}, {"VarCharValue": "beach.jpg"}]},
            ]
        }
    }
    rows = search._rows_from_results(result)
    assert rows == [{"file_id": "abc", "name": "beach.jpg"}]


def test_search_has_more_trimming(monkeypatch):
    rows = [{"file_id": str(i)} for i in range(21)]
    monkeypatch.setattr(search, "enrich_rows", lambda r: r)
    monkeypatch.setattr(search, "run_athena_query", lambda sql: rows)
    payload = search.search({"year": "all", "page_size": "20"})
    assert payload["count"] == 20
    assert payload["has_more"] is True
    assert len(payload["results"]) == 20


# --- presigned download --------------------------------------------------------

def test_presigned_download(monkeypatch):
    monkeypatch.setattr(
        search,
        "run_athena_query",
        lambda sql: [{"s3_raw_key": "raw/x.jpg", "original_filename": "beach.jpg", "media_type": "photo"}],
    )
    monkeypatch.setattr(search._s3, "generate_presigned_url", lambda **kw: "https://signed.example/beach.jpg")
    out = search.presigned_download("3f2504e0-4f89-41d3-9a0c-0305e82c3301")
    assert out["download_url"].startswith("https://")
    assert out["filename"] == "beach.jpg"


def test_catalog_matches_year_and_label():
    item = {
        "year": 2026,
        "month": 6,
        "uploader": "family",
        "rekognition_labels": ["Beach", "Person"],
        "media_type": "photo",
    }
    assert search._catalog_matches(item, {"year": "2026", "label": "Beach"})
    assert not search._catalog_matches(item, {"year": "2025"})
    assert not search._catalog_matches(item, {"year": "2026", "label": "Dog"})


def test_capture_date_parts():
    assert search._capture_date_parts("2014-02-05T13:24:58.000") == (2014, 2, 5)
    assert search._capture_date_parts("2014-02-05 13:24:58") == (2014, 2, 5)
    assert search._capture_date_parts(None) is None


def test_catalog_matches_capture_year_not_upload_year():
    # Uploaded 2026 but taken in 2014 (common for old camera imports).
    item = {
        "year": 2026,
        "month": 6,
        "capture_ts": "2014-02-05T13:24:58.000",
    }
    assert search._catalog_matches(item, {"when": "capture", "year": "2014"})
    assert not search._catalog_matches(item, {"when": "capture", "year": "2026"})
    assert search._catalog_matches(item, {"when": "upload", "year": "2026"})
    assert not search._catalog_matches(item, {"when": "upload", "year": "2014"})


def test_build_search_query_capture_year():
    sql, _, _ = search.build_search_query({"when": "capture", "year": "2014"})
    assert "year(capture_ts) = 2014" in sql
    assert "capture_ts IS NOT NULL" in sql


def test_catalog_search_pagination(monkeypatch):
    items = [
        {
            "file_id": f"id-{i}",
            "status": "enriched",
            "year": 2026,
            "upload_ts": f"2026-06-{i+1:02d}",
            "s3_thumbnail_key": f"thumbnails/x/{i}.jpg",
            "rekognition_labels": [],
        }
        for i in range(25)
    ]
    monkeypatch.setattr(search, "BROWSE_BACKEND", "dynamodb")
    monkeypatch.setattr(search, "_scan_enriched_catalog", lambda: items)
    page1 = search.catalog_search({"year": "2026", "page": "1", "page_size": "20"})
    assert page1["count"] == 20
    assert page1["has_more"] is True
    page2 = search.catalog_search({"year": "2026", "page": "2", "page_size": "20"})
    assert page2["count"] == 5
    assert page2["has_more"] is False


def test_presigned_download_invalid_uuid():
    with pytest.raises(ValueError):
        search.presigned_download("not-a-uuid")


# --- handler -------------------------------------------------------------------

class _FakeAthena:
    def start_query_execution(self, **kwargs):
        return {"QueryExecutionId": "q-1"}

    def get_query_execution(self, QueryExecutionId):  # noqa: N803
        return {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}

    def get_query_results(self, QueryExecutionId, MaxResults):  # noqa: N803
        return {
            "ResultSet": {
                "Rows": [
                    {
                        "Data": [
                            {"VarCharValue": "file_id"},
                            {"VarCharValue": "original_filename"},
                            {"VarCharValue": "uploader"},
                            {"VarCharValue": "upload_ts"},
                            {"VarCharValue": "capture_ts"},
                            {"VarCharValue": "media_type"},
                            {"VarCharValue": "rekognition_labels"},
                            {"VarCharValue": "s3_thumbnail_key"},
                            {"VarCharValue": "s3_raw_key"},
                        ]
                    },
                    {
                        "Data": [
                            {"VarCharValue": "3f2504e0-4f89-41d3-9a0c-0305e82c3301"},
                            {"VarCharValue": "beach.jpg"},
                            {"VarCharValue": "family"},
                            {"VarCharValue": "2026-06-10"},
                            {"VarCharValue": "2026-06-09"},
                            {"VarCharValue": "photo"},
                            {"VarCharValue": "[Beach, Person]"},
                            {"VarCharValue": "thumbnails/year=2026/month=06/day=10/f1.jpg"},
                            {"VarCharValue": "raw/year=2026/month=06/day=10/f1.jpg"},
                        ]
                    },
                ]
            }
        }


def test_handler_health():
    resp = search.handler({"rawPath": "/health", "requestContext": {"http": {"method": "GET"}}}, None)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["status"] == "ok"


def test_handler_search_success(monkeypatch):
    monkeypatch.setattr(search, "_athena", _FakeAthena())
    event = {
        "rawPath": "/search",
        "requestContext": {
            "http": {"method": "GET"},
            "authorizer": {"jwt": {"claims": {"sub": "user-1"}}},
        },
        "queryStringParameters": {"year": "2026", "page": "1"},
    }
    resp = search.handler(event, None)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["count"] == 1
    assert body["page"] == 1
    assert body["results"][0]["thumbnail_url"].startswith("https://")
    assert body["results"][0]["rekognition_labels"] == ["Beach", "Person"]


def test_handler_years(monkeypatch):
    monkeypatch.setattr(search, "run_athena_query", lambda sql: [{"year": "2026"}, {"year": "2025"}])
    event = {
        "rawPath": "/years",
        "requestContext": {"http": {"method": "GET"}, "authorizer": {"jwt": {"claims": {"sub": "u"}}}},
        "queryStringParameters": {"when": "upload"},
    }
    resp = search.handler(event, None)
    body = json.loads(resp["body"])
    assert body["years"] == [2026, 2025]
    assert body["when"] == "upload"


def test_handler_download(monkeypatch):
    monkeypatch.setattr(search, "presigned_download", lambda fid: {"download_url": "https://x", "filename": "a.jpg"})
    event = {
        "rawPath": "/download",
        "requestContext": {"http": {"method": "GET"}, "authorizer": {"jwt": {"claims": {"sub": "u"}}}},
        "queryStringParameters": {"file_id": "3f2504e0-4f89-41d3-9a0c-0305e82c3301"},
    }
    resp = search.handler(event, None)
    assert resp["statusCode"] == 200


def test_handler_search_bad_params():
    event = {
        "rawPath": "/search",
        "requestContext": {"http": {"method": "GET"}},
        "queryStringParameters": {"year": "2026", "media_type": "gif"},
    }
    resp = search.handler(event, None)
    assert resp["statusCode"] == 400
