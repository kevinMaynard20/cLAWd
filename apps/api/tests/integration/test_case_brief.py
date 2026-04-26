"""End-to-end case brief tests (spec §5.2, §6.1 L3).

Mocks the Anthropic SDK via the `set_anthropic_client_factory` injection point
so the test is deterministic without a real API key.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from data import db
from data.models import Block, BlockType, Book, Corpus, Page
from primitives import generate as generate_module

# ---------------------------------------------------------------------------
# Fake Anthropic client (mirrors anthropic.Anthropic surface used in generate)
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
            usage=_FakeUsage(input_tokens=1200, output_tokens=450),
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
def seeded_book(temp_env: None) -> tuple[str, str]:
    """Create a book with a Shelley v. Kraemer opinion + a couple of notes.
    Returns (corpus_id, block_id of the opinion)."""
    engine = db.get_engine()
    with Session(engine) as session:
        corpus = Corpus(name="Property", course="Property")
        session.add(corpus)
        session.commit()
        session.refresh(corpus)
        corpus_id = corpus.id

        book = Book(
            id="e" * 64,
            corpus_id=corpus_id,
            title="Property",
            source_pdf_path="/p.pdf",
            source_page_min=518,
            source_page_max=520,
        )
        session.add(book)
        session.commit()

        page = Page(
            book_id=book.id,
            source_page=518,
            batch_pdf="b.pdf",
            pdf_page_start=1000,
            pdf_page_end=1002,
            markdown="# page 518",
            raw_text="page 518",
        )
        session.add(page)
        session.commit()
        session.refresh(page)
        page_id = page.id

        opinion = Block(
            page_id=page_id,
            book_id=book.id,
            order_index=0,
            type=BlockType.CASE_OPINION,
            source_page=518,
            markdown=(
                "The issue presented is whether the judicial enforcement of "
                "restrictive covenants excluding persons of designated race from "
                "ownership of real property violates the Fourteenth Amendment."
            ),
            block_metadata={
                "case_name": "Shelley v. Kraemer",
                "court": "Supreme Court of the United States",
                "year": 1948,
                "citation": "334 U.S. 1",
            },
        )
        session.add(opinion)
        session.commit()
        session.refresh(opinion)
        opinion_id = opinion.id

        note = Block(
            page_id=page_id,
            book_id=book.id,
            order_index=1,
            type=BlockType.NUMBERED_NOTE,
            source_page=519,
            markdown="1. The state-action doctrine is central to this holding.",
            block_metadata={"number": 1, "has_problem": False},
        )
        session.add(note)
        session.commit()

    return corpus_id, opinion_id


# ---------------------------------------------------------------------------
# Valid case-brief JSON payload the mock returns
# ---------------------------------------------------------------------------


def _case_brief_payload(opinion_block_id: str) -> str:
    import json

    body = {
        "case_name": "Shelley v. Kraemer",
        "citation": "334 U.S. 1",
        "court": "Supreme Court of the United States",
        "year": 1948,
        "facts": [
            {
                "text": "A restrictive covenant barred Black purchasers from owning the property.",
                "source_block_ids": [opinion_block_id],
            }
        ],
        "procedural_posture": {
            "text": "State courts enforced the covenant; SCOTUS granted certiorari.",
            "source_block_ids": [opinion_block_id],
        },
        "issue": {
            "text": "Does judicial enforcement of a racially restrictive covenant violate the Fourteenth Amendment?",
            "source_block_ids": [opinion_block_id],
        },
        "holding": {
            "text": "Yes. State-court enforcement of such covenants is state action.",
            "source_block_ids": [opinion_block_id],
        },
        "rule": {
            "text": (
                "Judicial enforcement of private racially restrictive covenants "
                "constitutes state action subject to the Fourteenth Amendment."
            ),
            "source_block_ids": [opinion_block_id],
        },
        "reasoning": [
            {
                "text": "State courts acting as instrumentalities of the state cannot enforce racial exclusion.",
                "source_block_ids": [opinion_block_id],
            }
        ],
        "significance": {
            "text": "Established the state-action doctrine's application to judicial enforcement.",
            "source_block_ids": [opinion_block_id],
        },
        "where_this_fits": None,
        "limitations": [],
        "sources": [opinion_block_id],
    }
    return json.dumps(body)


# ---------------------------------------------------------------------------
# Tests — unit-level orchestration
# ---------------------------------------------------------------------------


def test_case_brief_happy_path_via_route(
    client: TestClient, seeded_book: tuple[str, str]
) -> None:
    corpus_id, opinion_id = seeded_book

    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _call_n: _case_brief_payload(opinion_id))
    )

    r = client.post(
        "/features/case-brief",
        json={"corpus_id": corpus_id, "block_id": opinion_id},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cache_hit"] is False
    assert body["verification_failed"] is False
    art = body["artifact"]
    assert art["type"] == "case_brief"
    assert art["content"]["case_name"] == "Shelley v. Kraemer"
    assert "Fourteenth Amendment" in art["content"]["rule"]["text"]
    # sources field surfaced the cited block id
    assert opinion_id in art["content"]["sources"]


def test_case_brief_cache_hit_second_call(
    client: TestClient, seeded_book: tuple[str, str]
) -> None:
    corpus_id, opinion_id = seeded_book

    calls = {"n": 0}

    def payload_fn(_call_n: int) -> str:
        calls["n"] += 1
        return _case_brief_payload(opinion_id)

    generate_module.set_anthropic_client_factory(_fake_factory(payload_fn))

    r1 = client.post(
        "/features/case-brief",
        json={"corpus_id": corpus_id, "block_id": opinion_id},
    )
    assert r1.status_code == 200
    r2 = client.post(
        "/features/case-brief",
        json={"corpus_id": corpus_id, "block_id": opinion_id},
    )
    assert r2.status_code == 200
    assert r1.json()["cache_hit"] is False
    assert r2.json()["cache_hit"] is True
    assert calls["n"] == 1  # Anthropic called only once


def test_case_brief_force_regenerate_bypasses_cache(
    client: TestClient, seeded_book: tuple[str, str]
) -> None:
    corpus_id, opinion_id = seeded_book
    calls = {"n": 0}

    def payload_fn(_call_n: int) -> str:
        calls["n"] += 1
        return _case_brief_payload(opinion_id)

    generate_module.set_anthropic_client_factory(_fake_factory(payload_fn))

    client.post(
        "/features/case-brief",
        json={"corpus_id": corpus_id, "block_id": opinion_id},
    )
    r = client.post(
        "/features/case-brief",
        json={
            "corpus_id": corpus_id,
            "block_id": opinion_id,
            "force_regenerate": True,
        },
    )
    assert r.status_code == 200
    assert r.json()["cache_hit"] is False
    assert calls["n"] == 2


def test_case_brief_404_on_unknown_case(
    client: TestClient, seeded_book: tuple[str, str]
) -> None:
    corpus_id, _ = seeded_book
    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _case_brief_payload("fake"))
    )
    r = client.post(
        "/features/case-brief",
        json={"corpus_id": corpus_id, "case_name": "Marbury v. Madison"},
    )
    assert r.status_code == 404


def test_case_brief_400_without_case_or_block(
    client: TestClient, seeded_book: tuple[str, str]
) -> None:
    corpus_id, _ = seeded_book
    r = client.post(
        "/features/case-brief", json={"corpus_id": corpus_id}
    )
    assert r.status_code == 400


def test_case_brief_402_when_budget_exceeded(
    client: TestClient,
    seeded_book: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus_id, opinion_id = seeded_book
    # Seed a CostEvent that blows past the cap.

    from costs import tracker

    monkeypatch.setenv("LAWSCHOOL_MONTHLY_CAP_USD", "0.01")
    tracker.record_llm_call(
        model="claude-opus-4-7",
        provider="anthropic",
        input_tokens=1000000,
        output_tokens=1000000,
        feature="test_seed",
    )

    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _case_brief_payload(opinion_id))
    )
    r = client.post(
        "/features/case-brief",
        json={"corpus_id": corpus_id, "block_id": opinion_id},
    )
    assert r.status_code == 402


def test_case_brief_emits_cost_event(
    client: TestClient, seeded_book: tuple[str, str]
) -> None:
    """Spec §7.7.4: every LLM call produces a CostEvent. Query the DB after
    and assert the row is there."""
    corpus_id, opinion_id = seeded_book
    generate_module.set_anthropic_client_factory(
        _fake_factory(lambda _n: _case_brief_payload(opinion_id))
    )
    client.post(
        "/features/case-brief",
        json={"corpus_id": corpus_id, "block_id": opinion_id},
    )
    events = client.get("/costs/events?feature=case_brief").json()
    assert events["count"] >= 1
    assert events["events"][0]["model"] == "claude-opus-4-7"
    assert float(events["events"][0]["total_cost_usd"]) > 0
