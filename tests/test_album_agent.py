"""Unit tests for the album_agent Lambda and the Step Functions definition."""
import io
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pyarrow.parquet as pq
import pytest

import album_agent_handler as agent

_ROOT = Path(__file__).resolve().parents[1]


# --- parse_albums_json ---------------------------------------------------------

def test_parse_albums_json_plain():
    text = '{"albums": [{"name": "Beach Week", "file_ids": ["a", "b", "c"]}]}'
    albums = agent.parse_albums_json(text)
    assert albums[0]["name"] == "Beach Week"


def test_parse_albums_json_with_fences_and_prose():
    text = 'Here you go!\n```json\n{"albums": [{"name": "X", "file_ids": []}]}\n```\nEnjoy.'
    assert agent.parse_albums_json(text)[0]["name"] == "X"


def test_parse_albums_json_rejects_garbage():
    with pytest.raises(ValueError):
        agent.parse_albums_json("I could not produce albums, sorry.")
    with pytest.raises(ValueError):
        agent.parse_albums_json('{"not_albums": 1}')


# --- validate_proposals ----------------------------------------------------------

def test_validate_proposals_drops_hallucinated_ids_and_small_albums():
    valid = {"a", "b", "c", "d"}
    albums = [
        {"name": "Good", "description": "d", "file_ids": ["a", "b", "c", "zzz"]},
        {"name": "TooSmallAfterFilter", "file_ids": ["a", "zzz", "yyy"]},
        {"name": "", "file_ids": ["a", "b", "c"]},
    ]
    cleaned = agent.validate_proposals(albums, valid)
    assert len(cleaned) == 1
    assert cleaned[0]["name"] == "Good"
    assert cleaned[0]["file_ids"] == ["a", "b", "c"]  # zzz dropped, sorted/deduped


def test_validate_proposals_truncates_long_names():
    albums = [{"name": "n" * 300, "file_ids": ["a", "b", "c"]}]
    cleaned = agent.validate_proposals(albums, {"a", "b", "c"})
    assert len(cleaned[0]["name"]) == 120


# --- compact_item ------------------------------------------------------------------

def test_compact_item_with_decimals_and_gps():
    item = {
        "file_id": "f1",
        "capture_ts": "2026-06-07T09:30:00+00:00",
        "uploader": "family",
        "media_type": "photo",
        "rekognition_labels": ["Beach", "Person", "Sea", "Sky", "Sand", "Water", "Sun", "Cloud", "Extra9"],
        "face_cluster_ids": ["abcdef12-3456-7890-abcd-ef1234567890"],
        "gps_lat": Decimal("40.4461234"),
        "gps_lon": Decimal("-3.7491234"),
    }
    out = agent.compact_item(item)
    assert out["date"] == "2026-06-07"
    assert len(out["labels"]) == 8  # capped
    assert out["people"] == ["abcdef12"]  # truncated cluster ids
    assert out["gps"] == [40.446, -3.749]


def test_compact_item_minimal():
    out = agent.compact_item({"file_id": "f2", "upload_ts": "2026-06-08T12:00:00+00:00"})
    assert out["file_id"] == "f2"
    assert out["date"] == "2026-06-08"
    assert "gps" not in out and "people" not in out


# --- resolve_cluster ------------------------------------------------------------------

def test_resolve_cluster_joins_existing():
    known = {"face-2": "cluster-A"}
    assert agent.resolve_cluster(["face-1", "face-2"], known) == "cluster-A"


def test_resolve_cluster_new_when_no_match():
    cluster = agent.resolve_cluster(["face-9"], {})
    assert len(cluster) == 36  # fresh uuid


# --- assignments parquet ----------------------------------------------------------------

def test_build_assignments_parquet_roundtrip():
    rows = [
        {
            "album_id": "alb-1",
            "album_name": "Beach Week",
            "file_id": "f1",
            "status": "proposed",
            "assigned_ts": datetime(2026, 6, 10, tzinfo=timezone.utc),
            "model_id": "us.anthropic.claude-3-5-haiku-20241022-v1:0",
        }
    ]
    table = pq.read_table(io.BytesIO(agent.build_assignments_parquet(rows)))
    assert table.num_rows == 1
    assert table.to_pylist()[0]["album_name"] == "Beach Week"


# --- handler: dispatch --------------------------------------------------------------------

def test_handler_rejects_unknown_action():
    with pytest.raises(ValueError):
        agent.handler({"action": "make_coffee"}, None)
    with pytest.raises(ValueError):
        agent.handler({}, None)


# --- fakes -----------------------------------------------------------------------------------

class _FakeCatalog:
    def __init__(self, items):
        self.items = items
        self.updates = []

    def scan(self, **kwargs):
        return {"Items": self.items}

    def update_item(self, **kwargs):
        self.updates.append(kwargs)


class _FakeFaces:
    def __init__(self, known=None):
        self.known = known or {}
        self.puts = []

    def get_item(self, Key):  # noqa: N803
        face_id = Key["face_id"]
        if face_id in self.known:
            return {"Item": {"face_id": face_id, "cluster_id": self.known[face_id]}}
        return {}

    def put_item(self, Item):  # noqa: N803
        self.puts.append(Item)


class _FakeRekognition:
    def __init__(self, faces_per_photo=1, match_face_id=None):
        self.faces_per_photo = faces_per_photo
        self.match_face_id = match_face_id
        self._counter = 0

    def index_faces(self, **kwargs):
        records = []
        for _ in range(self.faces_per_photo):
            self._counter += 1
            records.append({"Face": {"FaceId": f"face-{self._counter}"}})
        return {"FaceRecords": records}

    def search_faces(self, **kwargs):
        if self.match_face_id:
            return {"FaceMatches": [{"Face": {"FaceId": self.match_face_id}}]}
        return {"FaceMatches": []}


class _FakeS3:
    def __init__(self):
        self.put_calls = []

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)
        return {}


class _FakeBedrock:
    def __init__(self, albums):
        self.albums = albums
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "output": {"message": {"content": [{"text": json.dumps({"albums": self.albums})}]}},
            "usage": {"inputTokens": 1000, "outputTokens": 200},
        }


def _catalog_photo(file_id, **extra):
    item = {
        "file_id": file_id,
        "status": "enriched",
        "s3_raw_key": f"raw/year=2026/month=06/day=08/{file_id}_x.jpg",
        "media_type": "photo",
        "upload_ts": "2026-06-08T12:00:00+00:00",
        "rekognition_labels": ["Beach"],
        "face_count": 1,
        "file_size_bytes": Decimal("100"),
    }
    item.update(extra)
    return item


# --- handler: cluster_faces ------------------------------------------------------------------

def test_cluster_faces_assigns_same_cluster_on_match(monkeypatch):
    catalog = _FakeCatalog([_catalog_photo("f1"), _catalog_photo("f2")])
    faces = _FakeFaces()
    rek = _FakeRekognition(match_face_id="face-1")
    monkeypatch.setattr(agent, "_catalog", catalog)
    monkeypatch.setattr(agent, "_faces", faces)
    monkeypatch.setattr(agent, "_rekognition", rek)

    # After f1's face is stored, f2's match against face-1 must reuse its cluster.
    original_get = faces.get_item

    def tracking_get(Key):  # noqa: N803
        for put in faces.puts:
            if put["face_id"] == Key["face_id"]:
                return {"Item": put}
        return original_get(Key)

    faces.get_item = tracking_get

    result = agent.handler({"action": "cluster_faces"}, None)
    assert result["photos_indexed"] == 2
    assert result["faces_indexed"] == 2
    clusters = {p["cluster_id"] for p in faces.puts}
    assert len(clusters) == 1  # both photos -> same person cluster
    assert len(catalog.updates) == 2


def test_cluster_faces_no_candidates(monkeypatch):
    monkeypatch.setattr(agent, "_catalog", _FakeCatalog([]))
    result = agent.handler({"action": "cluster_faces"}, None)
    assert result == {"photos_indexed": 0, "faces_indexed": 0, "clusters_touched": 0}


# --- handler: propose_albums ------------------------------------------------------------------

def test_propose_albums_full_flow(monkeypatch):
    ids = [f"f{i}" for i in range(6)]
    catalog = _FakeCatalog([_catalog_photo(i) for i in ids])
    s3 = _FakeS3()
    bedrock = _FakeBedrock(
        albums=[{"name": "Beach Week", "description": "Sunny days", "file_ids": ids[:4]}]
    )
    monkeypatch.setattr(agent, "_catalog", catalog)
    monkeypatch.setattr(agent, "_s3", s3)
    monkeypatch.setattr(agent, "_bedrock", bedrock)

    result = agent.handler({"action": "propose_albums"}, None)

    assert result["albums"] == 1
    assert result["assigned"] == 4
    assert "Beach Week" in result["summary"]

    keys = [c["Key"] for c in s3.put_calls]
    manifest_keys = [k for k in keys if k.startswith("albums/") and k.endswith("manifest.json")]
    parquet_keys = [k for k in keys if k.startswith("album_assignments/")]
    assert len(manifest_keys) == 1 and len(parquet_keys) == 1

    manifest = json.loads(next(c for c in s3.put_calls if c["Key"] == manifest_keys[0])["Body"])
    assert manifest["status"] == "proposed"
    assert sorted(manifest["file_ids"]) == ids[:4]

    rows = pq.read_table(
        io.BytesIO(next(c for c in s3.put_calls if c["Key"] == parquet_keys[0])["Body"])
    ).to_pylist()
    assert len(rows) == 4

    # 4 catalog items got album_ids appended.
    assert len(catalog.updates) == 4


def test_propose_albums_skips_when_too_few_items(monkeypatch):
    catalog = _FakeCatalog([_catalog_photo("f1")])
    bedrock = _FakeBedrock(albums=[])
    monkeypatch.setattr(agent, "_catalog", catalog)
    monkeypatch.setattr(agent, "_bedrock", bedrock)

    result = agent.handler({"action": "propose_albums"}, None)
    assert result["skipped"] is True
    assert result["albums"] == 0
    assert bedrock.calls == []  # no Bedrock spend


def test_propose_albums_all_proposals_invalid(monkeypatch):
    ids = [f"f{i}" for i in range(6)]
    catalog = _FakeCatalog([_catalog_photo(i) for i in ids])
    s3 = _FakeS3()
    bedrock = _FakeBedrock(albums=[{"name": "Ghosts", "file_ids": ["nope1", "nope2", "nope3"]}])
    monkeypatch.setattr(agent, "_catalog", catalog)
    monkeypatch.setattr(agent, "_s3", s3)
    monkeypatch.setattr(agent, "_bedrock", bedrock)

    result = agent.handler({"action": "propose_albums"}, None)
    assert result["albums"] == 0
    assert s3.put_calls == []
    assert catalog.updates == []


# --- Step Functions definition sanity -----------------------------------------------------------

def test_asl_definition_is_valid_and_wired():
    asl = json.loads((_ROOT / "step_functions" / "nightly_agent.asl.json").read_text())
    states = asl["States"]

    assert asl["StartAt"] == "ClusterFaces"
    assert states["ClusterFaces"]["Parameters"]["Payload"]["action"] == "cluster_faces"
    assert states["ProposeAlbums"]["Parameters"]["Payload"]["action"] == "propose_albums"

    # Choice inspects the field propose_albums actually returns.
    choice = states["AnyProposals"]["Choices"][0]
    assert choice["Variable"] == "$.propose.result.albums"

    # SNS message uses the summary field.
    assert states["NotifyForApproval"]["Parameters"]["Message.$"] == "$.propose.result.summary"

    # Every Next/Default points at a real state.
    names = set(states)
    for state in states.values():
        for target in [state.get("Next"), state.get("Default")]:
            assert target is None or target in names
        for ch in state.get("Choices", []):
            assert ch["Next"] in names
