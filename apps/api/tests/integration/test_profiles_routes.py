"""Integration tests for /profiles + /ingest/past-exam (spec §5.13, §9 Phase 3).

Uses FastAPI's TestClient with the Anthropic SDK mocked via
`set_anthropic_client_factory` — same pattern as `test_case_brief.py`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from data import db
from data.models import Corpus
from primitives import generate as generate_module

# ---------------------------------------------------------------------------
# Fake Anthropic client
# ---------------------------------------------------------------------------


@dataclass
class _FakeUsage:
    input_tokens: int
    output_tokens: int


@dataclass
class _FakeTextContent:
    text: str
    type: str = "text"


@dataclass
class _FakeResponse:
    content: list[_FakeTextContent]
    usage: _FakeUsage
    model: str = "claude-opus-4-7"
    stop_reason: str = "end_turn"


class _FakeMessages:
    def __init__(self, payload_fn):
        self._payload_fn = payload_fn
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        payload = self._payload_fn(len(self.calls))
        return _FakeResponse(
            content=[_FakeTextContent(text=payload)],
            usage=_FakeUsage(input_tokens=1800, output_tokens=900),
        )


class _FakeClient:
    def __init__(self, payload_fn):
        self.messages = _FakeMessages(payload_fn)


def _fake_factory(payload_fn):
    def _factory(_api_key: str) -> _FakeClient:
        return _FakeClient(payload_fn)

    return _factory


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LAWSCHOOL_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("LAWSCHOOL_FORCE_FILE_BACKEND", "1")
    monkeypatch.setenv("LAWSCHOOL_CREDENTIALS_FILE", str(tmp_path / "creds.enc"))
    monkeypatch.delenv("LAWSCHOOL_MONTHLY_CAP_USD", raising=False)
    db.reset_engine()
    db.init_schema()

    from costs import tracker
    from credentials import keyring_backend

    tracker.reset_session_id()
    keyring_backend.store_anthropic_key("sk-ant-test-FAKEKEY-1234567890-LAST")

    yield
    generate_module.set_anthropic_client_factory(None)
    db.reset_engine()


@pytest.fixture
def client(temp_env: None) -> TestClient:
    from main import app

    return TestClient(app)


@pytest.fixture
def corpus_id(temp_env: None) -> str:
    engine = db.get_engine()
    with Session(engine) as session:
        c = Corpus(name="Property — Pollack — Spring 2026", course="Property")
        session.add(c)
        session.commit()
        session.refresh(c)
        return c.id


def _profile_payload(**overrides: Any) -> str:
    body = {
        "professor_name": overrides.get("professor_name", "Pollack"),
        "course": "Property",
        "school": "Benjamin N. Cardozo School of Law",
        "exam_format": {
            "duration_hours": 5.0,
            "word_limit": 4000,
            "open_book": False,
            "structure": [
                {"part": "I", "weight": 10, "type": "multiple_choice", "count": 10},
            ],
            "prompt_conventions": ["ambiguity closer"],
        },
        "pet_peeves": overrides.get(
            "pet_peeves",
            [
                {
                    "name": "hedge_without_resolution",
                    "pattern": "'it depends'",
                    "severity": "high",
                    "quote": None,
                    "source": "2023 memo p.2",
                }
            ],
        ),
        "favored_framings": ["Numerus clausus"],
        "stable_traps": [
            {
                "name": "deed_language_FSSEL_vs_FSD",
                "desc": "durational → FSSEL",
                "source": None,
            }
        ],
        "voice_conventions": [
            {"name": "prompt_role_varies", "desc": "clerk vs advocate"}
        ],
        "commonly_tested": ["RAP", "Takings"],
        "source_artifact_paths": ["/uploads/pollack_2023.md"],
    }
    return json.dumps(body)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_routes_past_exam_ingest(client: TestClient, corpus_id: str) -> None:
    """POST /ingest/past-exam returns both artifact ids."""
    r = client.post(
        "/ingest/past-exam",
        json={
            "corpus_id": corpus_id,
            "exam_markdown": "# 2023 Exam",
            "grader_memo_markdown": "Common errors: hedging.",
            "source_paths": ["/uploads/2023.md"],
            "year": 2023,
            "professor_name": "Pollack",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["past_exam_artifact_id"]
    assert body["grader_memo_artifact_id"]


def test_routes_past_exam_ingest_without_memo(
    client: TestClient, corpus_id: str
) -> None:
    r = client.post(
        "/ingest/past-exam",
        json={
            "corpus_id": corpus_id,
            "exam_markdown": "# 2025 Exam",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["past_exam_artifact_id"]
    assert body["grader_memo_artifact_id"] is None


def test_routes_past_exam_ingest_404_on_unknown_corpus(client: TestClient) -> None:
    r = client.post(
        "/ingest/past-exam",
        json={"corpus_id": "does-not-exist", "exam_markdown": "# x"},
    )
    assert r.status_code == 404


def test_routes_build_profile_happy_path(
    client: TestClient, corpus_id: str
) -> None:
    """End-to-end: ingest → build → check response includes non-empty details."""
    # First ingest a memo so there's something to extract from.
    r_ingest = client.post(
        "/ingest/past-exam",
        json={
            "corpus_id": corpus_id,
            "exam_markdown": "# 2023 Exam",
            "grader_memo_markdown": "Common errors: hedging.",
            "source_paths": ["/uploads/2023.md"],
            "year": 2023,
            "professor_name": "Pollack",
        },
    )
    assert r_ingest.status_code == 200
    memo_ids = [
        r_ingest.json()["past_exam_artifact_id"],
        r_ingest.json()["grader_memo_artifact_id"],
    ]

    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _profile_payload())
    )

    r = client.post(
        "/profiles/build",
        json={
            "corpus_id": corpus_id,
            "professor_name": "Pollack",
            "course": "Property",
            "school": "Benjamin N. Cardozo School of Law",
            "memo_artifact_ids": memo_ids,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cache_hit"] is False
    profile = body["profile"]
    assert profile["professor_name"] == "Pollack"
    assert profile["course"] == "Property"
    assert profile["pet_peeves"][0]["name"] == "hedge_without_resolution"
    assert profile["id"]


def test_routes_build_profile_no_memos_errors(
    client: TestClient, corpus_id: str
) -> None:
    """Zero usable memos → 503 (profile builder can't run)."""
    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _profile_payload())
    )
    r = client.post(
        "/profiles/build",
        json={
            "corpus_id": corpus_id,
            "professor_name": "Pollack",
            "course": "Property",
            "memo_artifact_ids": [],
        },
    )
    assert r.status_code == 503


def test_routes_get_profile_404_on_unknown_id(client: TestClient) -> None:
    r = client.get("/profiles/does-not-exist")
    assert r.status_code == 404


def test_routes_patch_profile_happy_path(
    client: TestClient, corpus_id: str
) -> None:
    """Seed → PATCH → GET shows update."""
    r_seed = client.post("/profiles/seed-pollack", json={"corpus_id": corpus_id})
    assert r_seed.status_code == 200
    pid = r_seed.json()["id"]

    patch_body = {
        "edits": {
            "commonly_tested": ["RAP", "Recording Acts", "Zoning", "Takings"]
        }
    }
    r_patch = client.patch(f"/profiles/{pid}", json=patch_body)
    assert r_patch.status_code == 200, r_patch.text
    assert "Recording Acts" in r_patch.json()["commonly_tested"]

    r_get = client.get(f"/profiles/{pid}")
    assert r_get.status_code == 200
    assert "Recording Acts" in r_get.json()["commonly_tested"]


def test_routes_patch_profile_schema_400(
    client: TestClient, corpus_id: str
) -> None:
    r_seed = client.post("/profiles/seed-pollack", json={"corpus_id": corpus_id})
    pid = r_seed.json()["id"]

    # Invalid severity value.
    bad = {
        "edits": {
            "pet_peeves": [
                {
                    "name": "x",
                    "pattern": "y",
                    "severity": "HUGE",
                    "source": "z",
                }
            ]
        }
    }
    r = client.patch(f"/profiles/{pid}", json=bad)
    assert r.status_code == 400


def test_routes_seed_pollack_endpoint(
    client: TestClient, corpus_id: str
) -> None:
    """POST /profiles/seed-pollack returns 200 and the profile exists."""
    r = client.post("/profiles/seed-pollack", json={"corpus_id": corpus_id})
    assert r.status_code == 200, r.text
    profile = r.json()
    assert profile["professor_name"] == "Pollack"
    assert profile["id"]

    # Calling again is idempotent: same id.
    r2 = client.post("/profiles/seed-pollack", json={"corpus_id": corpus_id})
    assert r2.status_code == 200
    assert r2.json()["id"] == profile["id"]

    # Also listable via GET /profiles?corpus_id=...
    r_list = client.get(f"/profiles?corpus_id={corpus_id}")
    assert r_list.status_code == 200
    ids = [p["id"] for p in r_list.json()]
    assert profile["id"] in ids


def test_routes_list_profiles_filter_by_name(
    client: TestClient, corpus_id: str
) -> None:
    client.post("/profiles/seed-pollack", json={"corpus_id": corpus_id})

    r_match = client.get(
        f"/profiles?corpus_id={corpus_id}&professor_name=Pollack"
    )
    assert r_match.status_code == 200
    assert len(r_match.json()) == 1

    r_miss = client.get(
        f"/profiles?corpus_id={corpus_id}&professor_name=Nobody"
    )
    assert r_miss.status_code == 200
    assert r_miss.json() == []
