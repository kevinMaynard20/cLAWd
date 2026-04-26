"""Unit tests for primitives/verify.py (spec §4.4).

Scope: the two rule-based profiles that land in Phase 2 —
``citation_grounding`` and ``rule_fidelity`` — plus coverage of the unknown-
profile raise and the two Phase-3 stubs.

The verifier reads the corpus DB, so every test seeds a small ``temp_db``
with one Book, a few Pages, and a few Blocks, then constructs in-memory
``Artifact`` objects (not persisted — the verifier takes an ``Artifact``
object, not an id).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import Session

from data import db
from data.models import (
    Artifact,
    ArtifactType,
    Block,
    BlockType,
    Book,
    Corpus,
    Page,
)
from primitives.verify import (
    VerificationIssue,
    VerificationResult,
    verify,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A fresh SQLite file per test. Mirrors test_models.py / test_retrieve.py."""
    monkeypatch.setenv("LAWSCHOOL_DB_PATH", str(tmp_path / "test.db"))
    db.reset_engine()
    db.init_schema()
    yield
    db.reset_engine()


@pytest.fixture
def seeded_corpus(temp_db: None) -> dict[str, str]:
    """Build one Book with three Blocks (A, B, C) and return their ids plus
    the corpus_id / book_id so tests can reference them explicitly.

    Block texts are chosen to give the rule_fidelity tests real material to
    overlap against — block A paraphrases the Takings doctrine, block B is
    a line of Shelley-style covenant language.
    """
    engine = db.get_engine()
    ids: dict[str, str] = {}
    with Session(engine) as session:
        corpus = Corpus(name="Property – Pollack", course="Property")
        session.add(corpus)
        session.commit()
        session.refresh(corpus)
        ids["corpus_id"] = corpus.id

        book = Book(
            id="b" * 64,
            corpus_id=corpus.id,
            title="Property: Cases and Materials",
            source_pdf_path="/p.pdf",
            source_page_min=518,
            source_page_max=519,
        )
        session.add(book)
        session.commit()
        ids["book_id"] = book.id

        page_518 = Page(
            book_id=book.id,
            source_page=518,
            batch_pdf="b.pdf",
            pdf_page_start=1036,
            pdf_page_end=1037,
            markdown="# Page 518",
            raw_text="Page 518",
        )
        page_519 = Page(
            book_id=book.id,
            source_page=519,
            batch_pdf="b.pdf",
            pdf_page_start=1038,
            pdf_page_end=1039,
            markdown="# Page 519",
            raw_text="Page 519",
        )
        session.add(page_518)
        session.add(page_519)
        session.commit()
        session.refresh(page_518)
        session.refresh(page_519)

        blk_a = Block(
            id="blk-A",
            page_id=page_518.id,
            book_id=book.id,
            order_index=0,
            type=BlockType.CASE_OPINION,
            source_page=518,
            markdown=(
                "A regulation that goes too far will be recognized as a "
                "taking. The categorical rule applies when the regulation "
                "denies all economically beneficial use of land."
            ),
            block_metadata={"case_name": "Pa. Coal v. Mahon"},
        )
        blk_b = Block(
            id="blk-B",
            page_id=page_518.id,
            book_id=book.id,
            order_index=1,
            type=BlockType.NUMBERED_NOTE,
            source_page=518,
            markdown=(
                "Judicial enforcement of racially restrictive covenants "
                "constitutes state action and violates the Fourteenth "
                "Amendment's Equal Protection Clause."
            ),
            block_metadata={"number": 1},
        )
        blk_c = Block(
            id="blk-C",
            page_id=page_519.id,
            book_id=book.id,
            order_index=0,
            type=BlockType.NARRATIVE_TEXT,
            source_page=519,
            markdown="Editorial commentary about the foregoing opinion.",
        )
        session.add_all([blk_a, blk_b, blk_c])
        session.commit()

        ids["blk_a"] = blk_a.id
        ids["blk_b"] = blk_b.id
        ids["blk_c"] = blk_c.id

    return ids


def _make_artifact(
    corpus_id: str,
    *,
    sources: list[dict] | None = None,
    content: dict | None = None,
    artifact_type: ArtifactType = ArtifactType.CASE_BRIEF,
) -> Artifact:
    """Build an in-memory Artifact for verification. Not persisted — verify()
    accepts the object directly."""
    return Artifact(
        corpus_id=corpus_id,
        type=artifact_type,
        sources=sources or [],
        content=content or {},
    )


# ---------------------------------------------------------------------------
# citation_grounding
# ---------------------------------------------------------------------------


def test_citation_grounding_happy_path(seeded_corpus: dict[str, str]) -> None:
    """Artifact declares blk-A in sources and cites only blk-A from content —
    verifier should return passed=True with zero issues."""
    artifact = _make_artifact(
        corpus_id=seeded_corpus["corpus_id"],
        sources=[{"kind": "block", "id": "blk-A"}],
        content={
            "facts": [
                {"text": "A Pennsylvania coal statute...", "source_block_ids": ["blk-A"]},
            ],
        },
    )
    result = verify(artifact, "citation_grounding")
    assert isinstance(result, VerificationResult)
    assert result.profile == "citation_grounding"
    assert result.passed is True
    assert result.issues == []
    assert result.soft_warnings == []


def test_citation_grounding_catches_source_not_in_declared_list(
    seeded_corpus: dict[str, str],
) -> None:
    """Content cites blk-X that's not in artifact.sources — expect an error."""
    artifact = _make_artifact(
        corpus_id=seeded_corpus["corpus_id"],
        sources=[{"kind": "block", "id": "blk-A"}],
        content={
            "facts": [{"text": "...", "source_block_ids": ["blk-X"]}],
        },
    )
    result = verify(artifact, "citation_grounding")
    assert result.passed is False
    errors = [i for i in result.issues if i.severity == "error"]
    assert any(
        "blk-X" in i.message and "artifact.sources" in i.message for i in errors
    ), f"expected missing-from-sources error, got: {[i.message for i in errors]}"
    # Context carries the machine-readable id for the retry layer.
    assert any(i.context.get("missing_block_id") == "blk-X" for i in errors)


def test_citation_grounding_catches_nonexistent_block(
    seeded_corpus: dict[str, str],
) -> None:
    """blk-Z is declared in sources but doesn't exist in the DB — error."""
    artifact = _make_artifact(
        corpus_id=seeded_corpus["corpus_id"],
        sources=[{"kind": "block", "id": "blk-Z"}],
        content={
            "facts": [{"text": "...", "source_block_ids": ["blk-Z"]}],
        },
    )
    result = verify(artifact, "citation_grounding")
    assert result.passed is False
    errors = [i for i in result.issues if i.severity == "error"]
    # The "not found in any book" error references blk-Z and the DB location.
    db_missing = [
        i
        for i in errors
        if i.context.get("missing_block_id") == "blk-Z"
        and i.context.get("location") == "database"
    ]
    assert db_missing, f"expected DB-missing error for blk-Z, got: {errors}"
    assert "blk-Z" in db_missing[0].message


def test_citation_grounding_traverses_nested_content(
    seeded_corpus: dict[str, str],
) -> None:
    """Nested facts/reasoning with mixed source_block_ids — all three are
    checked. If any is missing from sources we see errors for each; we
    deliberately declare all three in sources so we assert the happy-path
    traversal actually visited each id."""
    artifact = _make_artifact(
        corpus_id=seeded_corpus["corpus_id"],
        sources=[
            {"kind": "block", "id": "blk-A"},
            {"kind": "block", "id": "blk-B"},
            {"kind": "block", "id": "blk-C"},
        ],
        content={
            "facts": [
                {"text": "...", "source_block_ids": ["blk-A"]},
                {"text": "...", "source_block_ids": ["blk-B"]},
            ],
            "reasoning": [{"source_block_ids": ["blk-C"]}],
        },
    )
    result = verify(artifact, "citation_grounding")
    assert result.passed is True
    assert result.issues == []

    # Inverse assertion: if we drop blk-B / blk-C from sources, we get errors
    # for each. Proves the walker actually visited all three nested locations.
    artifact_partial = _make_artifact(
        corpus_id=seeded_corpus["corpus_id"],
        sources=[{"kind": "block", "id": "blk-A"}],  # blk-B, blk-C missing
        content=artifact.content,
    )
    partial = verify(artifact_partial, "citation_grounding")
    assert partial.passed is False
    missing_ids = {
        i.context.get("missing_block_id")
        for i in partial.issues
        if i.context.get("location") == "sources"
    }
    assert missing_ids == {"blk-B", "blk-C"}


# ---------------------------------------------------------------------------
# rule_fidelity
# ---------------------------------------------------------------------------


def test_rule_fidelity_high_overlap_passes(seeded_corpus: dict[str, str]) -> None:
    """Rule text is essentially the block's text with one word dropped;
    token coverage well above 60%. Expect passed=True, no warnings."""
    # blk-A markdown (abridged): "A regulation that goes too far will be
    # recognized as a taking. The categorical rule applies when the
    # regulation denies all economically beneficial use of land."
    rule_text = (
        "A regulation that goes too far will be recognized as a taking; "
        "the categorical rule applies when the regulation denies all "
        "economically beneficial use."
    )
    artifact = _make_artifact(
        corpus_id=seeded_corpus["corpus_id"],
        sources=[{"kind": "block", "id": "blk-A"}],
        content={"rule": {"text": rule_text, "source_block_ids": ["blk-A"]}},
    )
    result = verify(artifact, "rule_fidelity")
    assert result.passed is True
    assert result.issues == []
    assert result.soft_warnings == []


def test_rule_fidelity_low_overlap_warns(seeded_corpus: dict[str, str]) -> None:
    """Rule text uses vocabulary disjoint from the block. Still passed=True
    (warning severity), but soft_warnings surfaces the coverage percentage."""
    rule_text = (
        "Contractual privity between assignor and assignee governs "
        "enforcement of negative easements under ancient doctrine."
    )
    artifact = _make_artifact(
        corpus_id=seeded_corpus["corpus_id"],
        sources=[{"kind": "block", "id": "blk-A"}],
        content={"rule": {"text": rule_text, "source_block_ids": ["blk-A"]}},
    )
    result = verify(artifact, "rule_fidelity")
    # Warning, not error — spec §4.4 soft-attach branch.
    assert result.passed is True
    assert result.soft_warnings, "expected a soft warning for low overlap"
    warning = result.issues[0]
    assert warning.severity == "warning"
    assert "coverage" in warning.message
    assert "%" in warning.message
    # The human-readable message on `soft_warnings` is the same text.
    assert warning.message in result.soft_warnings


def test_rule_fidelity_paraphrase_marker_skips_check(
    seeded_corpus: dict[str, str],
) -> None:
    """Text marked `(paraphrase)` suppresses the overlap check entirely,
    even if the source block is completely unrelated."""
    rule_text = (
        "(paraphrase) Something wholly unrelated to coal regulation "
        "or covenants appears here."
    )
    artifact = _make_artifact(
        corpus_id=seeded_corpus["corpus_id"],
        sources=[{"kind": "block", "id": "blk-C"}],
        content={"rule": {"text": rule_text, "source_block_ids": ["blk-C"]}},
    )
    result = verify(artifact, "rule_fidelity")
    assert result.passed is True
    assert result.issues == [], (
        "paraphrase marker must suppress both errors AND warnings"
    )
    assert result.soft_warnings == []


def test_rule_fidelity_no_sources_is_error(seeded_corpus: dict[str, str]) -> None:
    """Rule has empty source_block_ids → hard error ('no source attribution')."""
    artifact = _make_artifact(
        corpus_id=seeded_corpus["corpus_id"],
        sources=[],
        content={"rule": {"text": "Some stated rule.", "source_block_ids": []}},
    )
    result = verify(artifact, "rule_fidelity")
    assert result.passed is False
    errors = [i for i in result.issues if i.severity == "error"]
    assert errors
    assert any("no source attribution" in i.message.lower() for i in errors)


# ---------------------------------------------------------------------------
# Unknown profile & Phase-3 stubs
# ---------------------------------------------------------------------------


def test_unknown_profile_raises_value_error(seeded_corpus: dict[str, str]) -> None:
    artifact = _make_artifact(corpus_id=seeded_corpus["corpus_id"])
    with pytest.raises(ValueError, match="Unknown verification profile"):
        verify(artifact, "not_a_real_profile")


def test_issue_spotting_completeness_rubric_passes(
    seeded_corpus: dict[str, str],
) -> None:
    """A well-formed RUBRIC artifact passes all rule-based checks."""
    artifact = _make_artifact(
        corpus_id=seeded_corpus["corpus_id"],
        artifact_type=ArtifactType.RUBRIC,
        content={
            "question_label": "Q1",
            "required_issues": [
                {"id": "i1", "label": "Issue one", "weight": 0.4, "why_required": "..."},
                {"id": "i2", "label": "Issue two", "weight": 0.3, "why_required": "..."},
                {"id": "i3", "label": "Issue three", "weight": 0.3, "why_required": "..."},
            ],
            "required_rules": [],
            "expected_counterarguments": [],
            "anti_patterns": [
                {"name": "clearly_as_argument_substitution", "pattern": "clearly", "severity": "high"},
            ],
            "prompt_role": "law clerk memo",
            "sources": [],
        },
    )
    result = verify(artifact, "issue_spotting_completeness")
    assert result.passed is True
    assert result.issues == []


def test_issue_spotting_completeness_hypo_passes(
    seeded_corpus: dict[str, str],
) -> None:
    """A well-formed HYPO artifact (with embedded rubric) passes."""
    artifact = _make_artifact(
        corpus_id=seeded_corpus["corpus_id"],
        artifact_type=ArtifactType.HYPO,
        content={
            "hypo": {"prompt": "...", "role": "law clerk memo", "word_limit": 2000},
            "rubric": {
                "question_label": "hypo_1",
                "required_issues": [
                    {"id": "i1", "label": "A", "weight": 0.5, "why_required": "..."},
                    {"id": "i2", "label": "B", "weight": 0.25, "why_required": "..."},
                    {"id": "i3", "label": "C", "weight": 0.25, "why_required": "..."},
                ],
                "required_rules": [],
                "expected_counterarguments": [],
                "anti_patterns": [{"name": "clearly", "pattern": "clearly", "severity": "high"}],
                "prompt_role": "law clerk memo",
                "sources": [],
            },
            "topics_covered": [],
            "sources": [],
        },
    )
    result = verify(artifact, "issue_spotting_completeness")
    assert result.passed is True


def test_issue_spotting_completeness_flags_thin_rubric(
    seeded_corpus: dict[str, str],
) -> None:
    """Fewer than 3 required_issues → warning."""
    artifact = _make_artifact(
        corpus_id=seeded_corpus["corpus_id"],
        artifact_type=ArtifactType.RUBRIC,
        content={
            "required_issues": [
                {"id": "i1", "label": "only one", "weight": 1.0, "why_required": "..."},
            ],
            "anti_patterns": [{"name": "x", "pattern": "x", "severity": "low"}],
            "prompt_role": "law clerk memo",
        },
    )
    result = verify(artifact, "issue_spotting_completeness")
    assert result.passed is True  # warning, not error
    assert any("required_issues" in w for w in result.soft_warnings)


def test_issue_spotting_completeness_flags_weights_off(
    seeded_corpus: dict[str, str],
) -> None:
    """Weights summing to far from 1.0 → warning."""
    artifact = _make_artifact(
        corpus_id=seeded_corpus["corpus_id"],
        artifact_type=ArtifactType.RUBRIC,
        content={
            "required_issues": [
                {"id": "i1", "label": "A", "weight": 0.1, "why_required": "..."},
                {"id": "i2", "label": "B", "weight": 0.1, "why_required": "..."},
                {"id": "i3", "label": "C", "weight": 0.1, "why_required": "..."},
            ],
            "anti_patterns": [{"name": "x", "pattern": "x", "severity": "low"}],
            "prompt_role": "law clerk memo",
        },
    )
    result = verify(artifact, "issue_spotting_completeness")
    assert any("weights sum to" in w for w in result.soft_warnings)


def test_issue_spotting_completeness_empty_label_is_error(
    seeded_corpus: dict[str, str],
) -> None:
    """An unlabeled issue breaks the grader — error severity."""
    artifact = _make_artifact(
        corpus_id=seeded_corpus["corpus_id"],
        artifact_type=ArtifactType.RUBRIC,
        content={
            "required_issues": [
                {"id": "i1", "label": "", "weight": 0.5, "why_required": "..."},
                {"id": "i2", "label": "B", "weight": 0.25, "why_required": "..."},
                {"id": "i3", "label": "C", "weight": 0.25, "why_required": "..."},
            ],
            "anti_patterns": [{"name": "x", "pattern": "x", "severity": "low"}],
            "prompt_role": "law clerk memo",
        },
    )
    result = verify(artifact, "issue_spotting_completeness")
    assert result.passed is False
    assert any(i.severity == "error" for i in result.issues)


def test_issue_spotting_completeness_rejects_wrong_artifact_type(
    seeded_corpus: dict[str, str],
) -> None:
    """CASE_BRIEF is not a valid input."""
    artifact = _make_artifact(
        corpus_id=seeded_corpus["corpus_id"],
        artifact_type=ArtifactType.CASE_BRIEF,
        content={},
    )
    result = verify(artifact, "issue_spotting_completeness")
    assert result.passed is False
    assert any("HYPO or RUBRIC" in i.message for i in result.issues)


def test_issue_spotting_completeness_hypo_missing_rubric_is_error(
    seeded_corpus: dict[str, str],
) -> None:
    """A HYPO whose content lacks the embedded rubric is broken."""
    artifact = _make_artifact(
        corpus_id=seeded_corpus["corpus_id"],
        artifact_type=ArtifactType.HYPO,
        content={"hypo": {"prompt": "..."}, "topics_covered": []},
    )
    result = verify(artifact, "issue_spotting_completeness")
    assert result.passed is False
    assert any("embedded 'rubric'" in i.message for i in result.issues)


# ---------------------------------------------------------------------------
# Small sanity checks for the dataclasses themselves
# ---------------------------------------------------------------------------


def test_verification_issue_is_frozen() -> None:
    """VerificationIssue is frozen so callers can't mutate a reported issue
    after the fact — keeps audit trails honest."""
    issue = VerificationIssue(
        severity="warning", profile="rule_fidelity", message="x"
    )
    with pytest.raises((AttributeError, TypeError)):
        issue.severity = "error"  # type: ignore[misc]


def test_verification_result_add_updates_passed_and_warnings() -> None:
    """VerificationResult.add() keeps passed/soft_warnings in sync."""
    result = VerificationResult(profile="citation_grounding", artifact_id="art-1")
    result.add(
        VerificationIssue(severity="warning", profile="citation_grounding", message="soft")
    )
    assert result.passed is True
    assert result.soft_warnings == ["soft"]
    result.add(
        VerificationIssue(severity="error", profile="citation_grounding", message="hard")
    )
    assert result.passed is False
    # Warnings list untouched by an error.
    assert result.soft_warnings == ["soft"]


# ---------------------------------------------------------------------------
# rubric_coverage (spec §4.4 + §5.5 Path A step 5)
# ---------------------------------------------------------------------------


def _seed_rubric(
    session: Session,
    corpus_id: str,
    *,
    required_issues: list[dict] | None = None,
    required_rules: list[dict] | None = None,
    expected_counterarguments: list[dict] | None = None,
) -> Artifact:
    """Persist a Rubric artifact for rubric_coverage tests. Returns the saved
    Artifact so callers can reference its id."""
    rubric = Artifact(
        corpus_id=corpus_id,
        type=ArtifactType.RUBRIC,
        content={
            "question_label": "Part II Q2",
            "required_issues": required_issues or [],
            "required_rules": required_rules or [],
            "expected_counterarguments": expected_counterarguments or [],
            "anti_patterns": [],
            "sources": [],
        },
    )
    session.add(rubric)
    session.commit()
    session.refresh(rubric)
    session.expunge(rubric)
    return rubric


def _make_grade(
    corpus_id: str,
    *,
    rubric_id: str | None,
    per_rubric_scores: list[dict] | None = None,
    artifact_type: ArtifactType = ArtifactType.GRADE,
) -> Artifact:
    """Build an in-memory Grade-shaped Artifact for rubric_coverage. The verify
    primitive accepts the object directly — no need to persist it."""
    content: dict = {"per_rubric_scores": per_rubric_scores or []}
    if rubric_id is not None:
        content["rubric_id"] = rubric_id
    return Artifact(
        corpus_id=corpus_id,
        type=artifact_type,
        sources=[],
        content=content,
    )


def test_rubric_coverage_happy_path(seeded_corpus: dict[str, str]) -> None:
    """Rubric with 3 required_issues, 2 required_rules, 1 counterargument; grade
    has entries for all 6. Should pass cleanly with no issues."""
    engine = db.get_engine()
    with Session(engine) as session:
        rubric = _seed_rubric(
            session,
            seeded_corpus["corpus_id"],
            required_issues=[
                {"id": "i1", "label": "Issue 1", "weight": 0.4, "why_required": "x"},
                {"id": "i2", "label": "Issue 2", "weight": 0.3, "why_required": "y"},
                {"id": "i3", "label": "Issue 3", "weight": 0.3, "why_required": "z"},
            ],
            required_rules=[
                {"id": "r1", "statement": "Rule 1", "tied_to_issues": ["i1"]},
                {"id": "r2", "statement": "Rule 2", "tied_to_issues": ["i2"]},
            ],
            expected_counterarguments=[
                {"id": "c1", "summary": "Argue in the alternative"},
            ],
        )

    grade = _make_grade(
        seeded_corpus["corpus_id"],
        rubric_id=rubric.id,
        per_rubric_scores=[
            {
                "rubric_item_id": "i1",
                "rubric_item_kind": "required_issue",
                "points_earned": 8.0,
                "points_possible": 10.0,
                "justification": "spotted and analyzed",
            },
            {
                "rubric_item_id": "i2",
                "rubric_item_kind": "required_issue",
                "points_earned": 6.0,
                "points_possible": 10.0,
                "justification": "partial",
            },
            {
                "rubric_item_id": "i3",
                "rubric_item_kind": "required_issue",
                "points_earned": 10.0,
                "points_possible": 10.0,
                "justification": "solid",
            },
            {
                "rubric_item_id": "r1",
                "rubric_item_kind": "required_rule",
                "points_earned": 5.0,
                "points_possible": 5.0,
                "justification": "stated and applied",
            },
            {
                "rubric_item_id": "r2",
                "rubric_item_kind": "required_rule",
                "points_earned": 4.0,
                "points_possible": 5.0,
                "justification": "stated",
            },
            {
                "rubric_item_id": "c1",
                "rubric_item_kind": "expected_counterargument",
                "points_earned": 2.0,
                "points_possible": 3.0,
                "justification": "acknowledged",
            },
        ],
    )

    result = verify(grade, "rubric_coverage")
    assert result.profile == "rubric_coverage"
    assert result.passed is True, f"unexpected issues: {result.issues}"
    assert result.issues == []
    assert result.soft_warnings == []


def test_rubric_coverage_missing_issue_score_is_error(
    seeded_corpus: dict[str, str],
) -> None:
    """Grade omits one required_issue → error emitted, missing id in context."""
    engine = db.get_engine()
    with Session(engine) as session:
        rubric = _seed_rubric(
            session,
            seeded_corpus["corpus_id"],
            required_issues=[
                {"id": "i1", "label": "Issue 1", "weight": 0.5, "why_required": "x"},
                {"id": "i2", "label": "Issue 2", "weight": 0.5, "why_required": "y"},
            ],
        )

    # Grade only scores i1 — i2 is missing.
    grade = _make_grade(
        seeded_corpus["corpus_id"],
        rubric_id=rubric.id,
        per_rubric_scores=[
            {
                "rubric_item_id": "i1",
                "rubric_item_kind": "required_issue",
                "points_earned": 5.0,
                "points_possible": 5.0,
                "justification": "solid",
            },
        ],
    )

    result = verify(grade, "rubric_coverage")
    assert result.passed is False
    errors = [i for i in result.issues if i.severity == "error"]
    assert errors, "expected an error for the missing rubric item"
    assert any(
        i.context.get("missing_rubric_item_id") == "i2" for i in errors
    ), f"expected missing_rubric_item_id=i2 in context, got: {[i.context for i in errors]}"
    assert any("i2" in i.message for i in errors)


def test_rubric_coverage_kind_mismatch_is_warning(
    seeded_corpus: dict[str, str],
) -> None:
    """Grade scores i1 as required_rule but rubric has i1 under required_issues
    → warning emitted, but result still passes (no error)."""
    engine = db.get_engine()
    with Session(engine) as session:
        rubric = _seed_rubric(
            session,
            seeded_corpus["corpus_id"],
            required_issues=[
                {"id": "i1", "label": "Issue 1", "weight": 1.0, "why_required": "x"},
            ],
        )

    grade = _make_grade(
        seeded_corpus["corpus_id"],
        rubric_id=rubric.id,
        per_rubric_scores=[
            {
                "rubric_item_id": "i1",
                "rubric_item_kind": "required_rule",  # mismatched
                "points_earned": 3.0,
                "points_possible": 5.0,
                "justification": "partial",
            },
        ],
    )

    result = verify(grade, "rubric_coverage")
    assert result.passed is True, (
        "kind mismatch must be a warning, not an error — still passes"
    )
    warnings = [i for i in result.issues if i.severity == "warning"]
    assert warnings, "expected a kind-mismatch warning"
    warning = warnings[0]
    assert warning.context.get("rubric_item_id") == "i1"
    assert warning.context.get("recorded_kind") == "required_rule"
    assert warning.context.get("expected_kind") == "required_issue"


def test_rubric_coverage_requires_grade_artifact_type(
    seeded_corpus: dict[str, str],
) -> None:
    """Passing a CASE_BRIEF artifact to rubric_coverage → error issue (not
    raised; the verifier returns a structured VerificationResult)."""
    artifact = Artifact(
        corpus_id=seeded_corpus["corpus_id"],
        type=ArtifactType.CASE_BRIEF,
        sources=[],
        content={"rubric_id": "anything", "per_rubric_scores": []},
    )
    result = verify(artifact, "rubric_coverage")
    assert result.passed is False
    errors = [i for i in result.issues if i.severity == "error"]
    assert errors
    assert any("Grade artifact" in i.message for i in errors)
    assert any(i.context.get("artifact_type") == "case_brief" for i in errors)


def test_rubric_coverage_missing_rubric_id_is_error(
    seeded_corpus: dict[str, str],
) -> None:
    """Grade.content has no rubric_id → error, no other checks possible."""
    grade = _make_grade(
        seeded_corpus["corpus_id"],
        rubric_id=None,
        per_rubric_scores=[],
    )
    result = verify(grade, "rubric_coverage")
    assert result.passed is False
    errors = [i for i in result.issues if i.severity == "error"]
    assert errors
    assert any("rubric_id" in i.message for i in errors)
    assert any(i.context.get("missing") == "content.rubric_id" for i in errors)


def test_rubric_coverage_nonexistent_rubric_id_is_error(
    seeded_corpus: dict[str, str],
) -> None:
    """rubric_id points to an artifact that doesn't exist → error."""
    grade = _make_grade(
        seeded_corpus["corpus_id"],
        rubric_id="nonexistent-rubric-id",
        per_rubric_scores=[],
    )
    result = verify(grade, "rubric_coverage")
    assert result.passed is False
    errors = [i for i in result.issues if i.severity == "error"]
    assert errors
    assert any(
        i.context.get("missing_rubric_id") == "nonexistent-rubric-id"
        for i in errors
    )
