"""Unit tests for the search_api Lambda."""
import json

import pytest

import search_handler as search


# --- build_search_query --------------------------------------------------------

def test_build_search_query_year_only():
    sql, limit = search.build_search_query({"year": "2026"})
    assert "year = 2026" in sql
    assert "month" not in sql
    assert limit == search.DEFAULT_LIMIT


def test_build_search_query_full_filters():
    sql, limit = search.build_search_query(
        {
            "year": "2026",
            "month": "6",
            "day": "10",
            "label": "Beach",
            "uploader": "ariel",
            "media_type": "photo",
            "limit": "25",
        }
    )
    assert "year = 2026" in sql
    assert "month = 6" in sql
    assert "day = 10" in sql
    assert "contains(rekognition_labels, 'Beach')" in sql
    assert "uploader = 'ariel'" in sql
    assert "media_type = 'photo'" in sql
    assert "LIMIT 25" in sql
    assert limit == 25


def test_build_search_query_accepts_q_alias():
    sql, _ = search.build_search_query({"year": "2026", "q": "Dog"})
    assert "contains(rekognition_labels, 'Dog')" in sql


def test_build_search_query_rejects_bad_uploader():
    with pytest.raises(ValueError, match="uploader"):
        search.build_search_query({"year": "2026", "uploader": "bad;drop"})


def test_build_search_query_escapes_quotes_in_label():
    sql, _ = search.build_search_query({"year": "2026", "label": "Kid's Party"})
    assert "Kid''s Party" in sql


# --- enrich helpers ------------------------------------------------------------

def test_thumbnail_url():
    url = search.thumbnail_url("thumbnails/year=2026/month=06/day=10/x.jpg")
    assert url == "https://d111111abcdef8.cloudfront.net/thumbnails/year=2026/month=06/day=10/x.jpg"


def test_parse_label_array():
    assert search._parse_label_array("[Beach, Person]") == ["Beach", "Person"]
    assert search._parse_label_array("[]") == []


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


# --- handler -------------------------------------------------------------------

class _FakeAthena:
    def __init__(self, rows):
        self.rows = rows
        self.started = []

    def start_query_execution(self, **kwargs):
        self.started.append(kwargs)
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
                        ]
                    },
                    {
                        "Data": [
                            {"VarCharValue": "f1"},
                            {"VarCharValue": "beach.jpg"},
                            {"VarCharValue": "ariel"},
                            {"VarCharValue": "2026-06-10"},
                            {"VarCharValue": "2026-06-09"},
                            {"VarCharValue": "photo"},
                            {"VarCharValue": "[Beach, Person]"},
                            {"VarCharValue": "thumbnails/year=2026/month=06/day=10/f1.jpg"},
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
    monkeypatch.setattr(search, "_athena", _FakeAthena([]))
    event = {
        "rawPath": "/search",
        "requestContext": {
            "http": {"method": "GET"},
            "authorizer": {"jwt": {"claims": {"sub": "user-1"}}},
        },
        "queryStringParameters": {"year": "2026", "month": "6"},
    }
    resp = search.handler(event, None)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["count"] == 1
    assert body["results"][0]["thumbnail_url"].startswith("https://")
    assert body["results"][0]["rekognition_labels"] == ["Beach", "Person"]


def test_handler_search_bad_params():
    event = {
        "rawPath": "/search",
        "requestContext": {"http": {"method": "GET"}},
        "queryStringParameters": {"year": "2026", "media_type": "gif"},
    }
    resp = search.handler(event, None)
    assert resp["statusCode"] == 400
