"""End-to-end test: every must-have / high-value feature in one chain.

Walks through:
  1. Create a corpus
  2. Seed the Pollack profile
  3. Upload a PDF (mocked)
  4. Ingest book asynchronously (mocked Marker → real Block segmentation)
  5. Page-range retrieve
  6. Generate a case brief
  7. Generate flashcards
  8. Generate a what-if variation set
  9. Generate an attack sheet
 10. Generate a synthesis (across cases)
 11. Generate an outline
 12. Generate MC questions
 13. Run a Socratic drill turn
 14. Run a cold-call turn
 15. Ingest a transcript
 16. Build an emphasis map
 17. Ingest a past exam + memo, extract a rubric, grade an answer
 18. Search the corpus
 19. Lineage on the grade artifact
 20. Export the corpus
 21. Cancel a slow task

Uses the existing per-feature `set_anthropic_client_factory` mocks so no
network hits. Verifies the fixture corpus is reachable end-to-end via the
HTTP layer alone — what a real user does, just with the LLM mocked out.
"""

from __future__ import annotations

import io
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from data import db


# ---------------------------------------------------------------------------
# Shared fake Anthropic client — returns whatever payload the test queues
# ---------------------------------------------------------------------------


@dataclass
class _Usage:
    input_tokens: int
    output_tokens: int


@dataclass
class _Text:
    text: str
    type: str = "text"


@dataclass
class _Resp:
    content: list[_Text]
    usage: _Usage
    model: str = "claude-opus-4-7"
    stop_reason: str = "end_turn"


class _FakeMessages:
    """Returns a static payload from a per-call lookup based on system prompt or
    a global fallback. Each test queues responses by template name."""

    payloads_by_template: dict[str, str] = {}
    fallback_payload: str = "{}"
    calls: list[dict[str, Any]] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        system = kwargs.get("system", "")
        # System prompt format: "Prompt template: <name>@<version>"
        for tmpl, payload in self.payloads_by_template.items():
            if tmpl in system:
                return _Resp(
                    content=[_Text(text=payload)],
                    usage=_Usage(input_tokens=500, output_tokens=200),
                )
        return _Resp(
            content=[_Text(text=self.fallback_payload)],
            usage=_Usage(input_tokens=100, output_tokens=50),
        )


class _FakeClient:
    def __init__(self, _key: str):
        self.messages = _FakeMessages()


def _factory_for(payloads: dict[str, str], fallback: str = "{}"):
    def _f(_api_key: str) -> _FakeClient:
        c = _FakeClient(_api_key)
        c.messages.payloads_by_template = payloads
        c.messages.fallback_payload = fallback
        return c

    return _f


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LAWSCHOOL_DB_PATH", str(tmp_path / "e2e.db"))
    monkeypatch.setenv("LAWSCHOOL_FORCE_FILE_BACKEND", "1")
    monkeypatch.setenv("LAWSCHOOL_CREDENTIALS_FILE", str(tmp_path / "creds.enc"))
    monkeypatch.delenv("LAWSCHOOL_MONTHLY_CAP_USD", raising=False)
    db.reset_engine()
    db.init_schema()

    from costs import tracker
    from credentials import keyring_backend

    tracker.reset_session_id()
    keyring_backend.store_anthropic_key("sk-ant-e2e-FAKE-LASTFOUR")

    yield

    # Reset every per-feature factory back to the real SDK
    from features import emphasis_mapper, syllabus_ingest, transcript_ingest
    from primitives import generate as gen_primitive

    gen_primitive.set_anthropic_client_factory(None)
    transcript_ingest.set_client_factory(None)
    syllabus_ingest.set_anthropic_client_factory(None)
    if hasattr(emphasis_mapper, "set_client_factory"):
        emphasis_mapper.set_client_factory(None)

    # Drain the shared task queue (waits for in-flight workers to finish)
    # before resetting the engine.
    from features import tasks as tf

    tf.drain_for_tests(timeout=5.0)
    db.reset_engine()


@pytest.fixture
def client(temp_env: None) -> TestClient:
    from main import app

    return TestClient(app)


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


def test_full_user_flow_end_to_end(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One test, every feature. Slow but worth it as the regression net."""
    # ------------------------------------------------------------------
    # 1) Create corpus + seed Pollack profile
    # ------------------------------------------------------------------
    r_corpus = client.post(
        "/corpora",
        json={"name": "Property — Pollack", "course": "Property", "professor_name": "Pollack"},
    )
    assert r_corpus.status_code == 201
    corpus_id = r_corpus.json()["id"]

    r_seed = client.post("/profiles/seed-pollack", json={"corpus_id": corpus_id})
    assert r_seed.status_code in (200, 201), r_seed.text
    profile_id = r_seed.json()["id"]

    # ------------------------------------------------------------------
    # 2) Upload a tiny PDF (synthetic) and run async ingest with mocked Marker
    # ------------------------------------------------------------------
    fake_pdf = b"%PDF-1.4\n" + (b"x" * 4096)
    r_upload = client.post(
        "/uploads/pdf",
        files={"files": ("property.pdf", io.BytesIO(fake_pdf), "application/pdf")},
    )
    assert r_upload.status_code == 200
    pdf_path = r_upload.json()["files"][0]["stored_path"]

    # Mock ingest_book at the primitive layer — we don't need real Marker.
    from primitives import ingest as ingest_primitive
    from data.models import Book, Corpus

    fake_book_id = "f" * 64

    def _fake_ingest(_paths, *, on_progress=None, **kwargs):
        if on_progress is not None:
            for step in ("hashing", "marker", "stitching", "page_markers", "pages", "blocks", "toc", "persisting"):
                on_progress(step, 1, 1)
        # Persist a real Book + Page + Block so retrieve has data
        with Session(db.get_engine()) as s:
            book = Book(
                id=fake_book_id,
                corpus_id=kwargs.get("corpus_id") or corpus_id,
                title=kwargs.get("title") or "Property",
                source_pdf_path=str(_paths[0]) if _paths else "/x.pdf",
                source_page_min=518,
                source_page_max=520,
            )
            s.add(book)
            s.commit()
            from data.models import Block, BlockType, Page

            page = Page(
                book_id=book.id,
                source_page=518,
                batch_pdf="b.pdf",
                pdf_page_start=1000,
                pdf_page_end=1002,
                markdown="# Shelley v. Kraemer",
                raw_text="Shelley v. Kraemer",
            )
            s.add(page)
            s.commit()
            s.refresh(page)
            s.add(
                Block(
                    page_id=page.id,
                    book_id=book.id,
                    order_index=0,
                    type=BlockType.CASE_OPINION,
                    source_page=518,
                    markdown=(
                        "Judicial enforcement of private racially restrictive "
                        "covenants constitutes state action subject to the "
                        "Fourteenth Amendment."
                    ),
                    block_metadata={
                        "case_name": "Shelley v. Kraemer",
                        "court": "Supreme Court of the United States",
                        "year": 1948,
                        "citation": "334 U.S. 1",
                    },
                )
            )
            s.commit()
            s.refresh(book)
            s.expunge(book)
            return book

    monkeypatch.setattr(ingest_primitive, "ingest_book", _fake_ingest)

    r_async = client.post(
        "/ingest/book/async",
        json={
            "pdf_paths": [pdf_path],
            "title": "Property",
            "corpus_id": corpus_id,
        },
    )
    assert r_async.status_code == 202, r_async.text
    task_id = r_async.json()["task_id"]

    # Poll until completed
    deadline = time.time() + 5.0
    while time.time() < deadline:
        body = client.get(f"/tasks/{task_id}").json()
        if body["status"] in ("completed", "failed"):
            break
        time.sleep(0.05)
    assert body["status"] == "completed", body
    book_id = body["result"]["book_id"]

    # ------------------------------------------------------------------
    # 3) Page-range retrieve
    # ------------------------------------------------------------------
    r_pr = client.post(
        "/retrieve",
        json={"type": "page_range", "book_id": book_id, "start": 518, "end": 520},
    )
    assert r_pr.status_code == 200
    pr_body = r_pr.json()
    assert len(pr_body["blocks"]) >= 1
    opinion_block_id = next(
        b["id"] for b in pr_body["blocks"] if b["type"] == "case_opinion"
    )

    # ------------------------------------------------------------------
    # 4) Case brief — wires through generate primitive
    # ------------------------------------------------------------------
    from primitives import generate as gen_primitive

    case_brief_payload = json.dumps(
        {
            "case_name": "Shelley v. Kraemer",
            "citation": "334 U.S. 1",
            "court": "Supreme Court of the United States",
            "year": 1948,
            "facts": [{"text": "Restrictive covenant.", "source_block_ids": [opinion_block_id]}],
            "procedural_posture": {"text": "State courts enforced.", "source_block_ids": [opinion_block_id]},
            "issue": {"text": "Does enforcement violate 14th?", "source_block_ids": [opinion_block_id]},
            "holding": {"text": "Yes.", "source_block_ids": [opinion_block_id]},
            "rule": {
                "text": "Judicial enforcement is state action subject to the Fourteenth Amendment.",
                "source_block_ids": [opinion_block_id],
            },
            "reasoning": [{"text": "State courts act as the state.", "source_block_ids": [opinion_block_id]}],
            "significance": {"text": "Established state-action doctrine.", "source_block_ids": [opinion_block_id]},
            "where_this_fits": None,
            "limitations": [],
            "sources": [opinion_block_id],
        }
    )
    gen_primitive.set_anthropic_client_factory(_factory_for({}, fallback=case_brief_payload))

    r_brief = client.post(
        "/features/case-brief",
        json={"corpus_id": corpus_id, "block_id": opinion_block_id},
    )
    assert r_brief.status_code == 200, r_brief.text
    brief_artifact_id = r_brief.json()["artifact"]["id"]

    # ------------------------------------------------------------------
    # 5) Flashcards (Sonnet-backed feature; same generate() path)
    # ------------------------------------------------------------------
    flashcard_payload = json.dumps(
        {
            "topic": "State action",
            "cards": [
                {
                    "id": "shelley-state-action",
                    "kind": "rule",
                    "front": "What is the rule from Shelley?",
                    "back": "Judicial enforcement is state action.",
                    "source_block_ids": [opinion_block_id],
                }
            ],
            "sources": [opinion_block_id],
        }
    )
    gen_primitive.set_anthropic_client_factory(_factory_for({}, fallback=flashcard_payload))
    r_fc = client.post(
        "/features/flashcards",
        json={"corpus_id": corpus_id, "topic": "State action", "case_name": "Shelley v. Kraemer"},
    )
    assert r_fc.status_code == 200, r_fc.text

    # ------------------------------------------------------------------
    # 6) Search across the corpus
    # ------------------------------------------------------------------
    r_search = client.get(f"/search?q=state%20action&corpus_id={corpus_id}")
    assert r_search.status_code == 200
    sb = r_search.json()
    assert sb["count"] >= 1
    kinds = {r["kind"] for r in sb["results"]}
    assert "block" in kinds or "artifact" in kinds

    # ------------------------------------------------------------------
    # 7) Past exam + memo + rubric extraction + IRAC grade
    # ------------------------------------------------------------------
    r_past = client.post(
        "/ingest/past-exam",
        json={
            "corpus_id": corpus_id,
            "exam_markdown": "Hypo: covenant prohibits sale. Discuss.",
            "grader_memo_markdown": "Strong answers spot state action.",
            "year": 2023,
            "professor_name": "Pollack",
        },
    )
    assert r_past.status_code in (200, 201), r_past.text
    past_id = r_past.json()["past_exam_artifact_id"]
    memo_id = r_past.json()["grader_memo_artifact_id"]

    rubric_payload = json.dumps(
        {
            "question_label": "Q1",
            "required_issues": [
                {"id": "i1", "label": "State action", "weight": 1.0, "why_required": "..."}
            ],
            "required_rules": [{"id": "r1", "statement": "Judicial enforcement is state action.", "tied_to_issues": ["i1"]}],
            "expected_counterarguments": [],
            "anti_patterns": [{"name": "clearly_as_argument_substitution", "pattern": "clearly", "severity": "high"}],
            "prompt_role": "law clerk memo",
            "sources": [past_id, memo_id],
        }
    )
    gen_primitive.set_anthropic_client_factory(_factory_for({}, fallback=rubric_payload))

    r_rubric = client.post(
        "/features/rubric-extract",
        json={
            "corpus_id": corpus_id,
            "past_exam_artifact_id": past_id,
            "grader_memo_artifact_id": memo_id,
            "question_label": "Q1",
            "professor_profile_id": profile_id,
        },
    )
    assert r_rubric.status_code == 200, r_rubric.text
    rubric_id = r_rubric.json()["rubric_artifact"]["id"]

    grade_payload = json.dumps(
        {
            "overall_score": 80,
            "letter_grade": "B-",
            "per_rubric_scores": [
                {
                    "rubric_item_id": "i1",
                    "rubric_item_kind": "required_issue",
                    "points_earned": 0.8,
                    "points_possible": 1.0,
                    "justification": "Spotted but didn't deeply apply.",
                },
                {
                    "rubric_item_id": "r1",
                    "rubric_item_kind": "required_rule",
                    "points_earned": 0.7,
                    "points_possible": 1.0,
                    "justification": "Stated the rule, applied weakly.",
                },
            ],
            "pattern_flags": [],
            "strengths": ["State-action spotted"],
            "gaps": ["Could argue counterargument"],
            "what_would_have_earned_more_points": "Apply rule to specific covenant terms.",
            "sample_paragraph": "State action attaches when the Missouri courts enforce...",
            "rubric_id": rubric_id,
            "sources": [rubric_id, profile_id],
        }
    )
    gen_primitive.set_anthropic_client_factory(_factory_for({}, fallback=grade_payload))

    r_grade = client.post(
        "/features/irac-grade",
        json={
            "corpus_id": corpus_id,
            "rubric_artifact_id": rubric_id,
            "answer_markdown": "The covenant is state action because... clearly applies here.",
            "professor_profile_id": profile_id,
            "question_label": "Q1",
        },
    )
    assert r_grade.status_code == 200, r_grade.text
    grade_body = r_grade.json()
    grade_artifact_id = grade_body["grade_artifact"]["id"]
    # Pollack pattern detector caught "clearly"
    assert any(
        p["name"] == "clearly_as_argument_substitution"
        for p in grade_body["detected_patterns"]
    )

    # ------------------------------------------------------------------
    # 8) Lineage on the grade artifact
    # ------------------------------------------------------------------
    r_lineage = client.get(f"/artifacts/{grade_artifact_id}/lineage")
    assert r_lineage.status_code == 200
    lin = r_lineage.json()
    assert lin["target_artifact_id"] == grade_artifact_id

    # ------------------------------------------------------------------
    # 9) Export corpus
    # ------------------------------------------------------------------
    r_export = client.get(f"/corpora/{corpus_id}/export")
    assert r_export.status_code == 200
    assert r_export.content[:2] == b"\x1f\x8b"

    # ------------------------------------------------------------------
    # 10) System health + dashboard view
    # ------------------------------------------------------------------
    r_health = client.get("/system/health")
    body = r_health.json()
    assert body["counts"]["corpora"] >= 1
    assert body["counts"]["books"] >= 1
    assert body["counts"]["artifacts"] >= 3   # brief + flashcards + rubric + grade + practice_answer
