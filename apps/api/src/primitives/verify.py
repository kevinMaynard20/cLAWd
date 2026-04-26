"""Primitive 4: Verify (spec §4.4).

Post-generation verification of critical-path outputs. The verifier inspects an
Artifact (generated or user-authored) and cross-checks its claims against the
corpus, flagging hallucinated citations, materially-deviated rules, and the
like. Output is a ``VerificationResult`` with per-issue severity; callers
decide whether to soft-attach warnings or trigger a hard retry of generation
(spec §4.4).

Phase 2 scope — rule-based profiles, no LLM calls:

- ``citation_grounding`` — every block id cited in ``artifact.content`` resolves
  to (a) the artifact's declared ``sources`` list, and (b) a real ``Block`` row
  in the DB.
- ``rule_fidelity`` — case_brief-specific: the stated rule overlaps materially
  with the source blocks' text (token overlap >= 60% after stopword removal),
  unless the rule itself is marked ``(paraphrase)``.

Phase 3 profiles:

- ``rubric_coverage`` — spec §5.5 Path A step 5; every rubric item in the
  referenced Rubric artifact must appear in the Grade's ``per_rubric_scores``.

Phase 3 stubs that raise ``NotImplementedError`` with a pointer back to spec:

- ``issue_spotting_completeness`` — spec §5.5 Path B step 3; needs a second
  LLM pass.

Unknown profiles raise ``ValueError``.

Design notes:

- The verifier reads the DB through a caller-provided ``Session`` (mirrors
  ``primitives.retrieve``) so it composes with FastAPI's dependency injection
  and with CLI workers that own their own transaction. If no session is
  provided, we open one via ``data.db.session_scope()`` for the call.
- ``VerificationIssue`` is frozen (immutable); ``VerificationResult`` is
  mutable so dispatchers can accumulate issues.
- No LLM calls in this module — that's the whole point of Phase 2's verifier.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlmodel import Session, select

from data.db import session_scope
from data.models import Artifact, ArtifactType, Block

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


Severity = Literal["warning", "error"]


@dataclass(frozen=True)
class VerificationIssue:
    """One finding from a verify() call.

    Severity semantics (spec §4.4):
        - ``error``: grounds for a hard retry of generation with the verifier's
          feedback fed back in. Sets ``VerificationResult.passed = False``.
        - ``warning``: soft-attach to the artifact for human review; does not
          flip ``passed``. Legitimate paraphrase or borderline coverage is a
          warning, not an error.

    ``context`` carries machine-readable detail (e.g. ``{"missing_block_id":
    "blk-42"}``) so UI / retry layers can act on the issue without parsing the
    human-readable ``message``.
    """

    severity: Severity
    profile: str
    message: str
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class VerificationResult:
    """Output of ``verify()``. Mutable so dispatchers can accumulate.

    ``passed`` is True iff no issue has severity ``error`` — warnings do not
    fail verification. ``soft_warnings`` surfaces the warning messages as
    plain strings for convenient attachment to an artifact's metadata.
    """

    profile: str
    artifact_id: str
    issues: list[VerificationIssue] = field(default_factory=list)
    passed: bool = True
    soft_warnings: list[str] = field(default_factory=list)

    def add(self, issue: VerificationIssue) -> None:
        """Append an issue, updating derived fields accordingly."""
        self.issues.append(issue)
        if issue.severity == "error":
            self.passed = False
        else:
            self.soft_warnings.append(issue.message)


# ---------------------------------------------------------------------------
# Public dispatch
# ---------------------------------------------------------------------------


KNOWN_PROFILES = (
    "citation_grounding",
    "rule_fidelity",
    "rubric_coverage",
    "issue_spotting_completeness",
)


def verify(
    artifact: Artifact,
    profile: str,
    *,
    session: Session | None = None,
) -> VerificationResult:
    """Run the named verification profile against ``artifact``.

    Args:
        artifact: an ``Artifact`` object (not an id — verifying accepts the
            in-memory object since generation produces one before persist).
        profile: one of the profile names in ``KNOWN_PROFILES``.
        session: reuse an existing ``Session`` if the caller has one; else the
            verifier opens a ``session_scope()`` for the call.

    Raises:
        ValueError: unknown profile name.
        NotImplementedError: for the remaining Phase-3 stub profile
            (``issue_spotting_completeness``).
    """
    if profile == "issue_spotting_completeness":
        return _issue_spotting_completeness_rule_based(artifact)

    if profile not in ("citation_grounding", "rule_fidelity", "rubric_coverage"):
        raise ValueError(
            f"Unknown verification profile: {profile!r}. "
            f"Known: citation_grounding, rule_fidelity, rubric_coverage."
        )

    # Session handling: reuse caller's session or open a scoped one.
    if session is not None:
        return _dispatch(artifact, profile, session)

    with session_scope() as owned_session:
        return _dispatch(artifact, profile, owned_session)


def _dispatch(artifact: Artifact, profile: str, session: Session) -> VerificationResult:
    if profile == "citation_grounding":
        return _verify_citation_grounding(artifact, session)
    if profile == "rule_fidelity":
        return _verify_rule_fidelity(artifact, session)
    if profile == "rubric_coverage":
        return _verify_rubric_coverage(artifact, session)
    # Unreachable — the ``verify()`` gate already filtered other profiles.
    raise ValueError(f"Unknown verification profile: {profile!r}")


# ---------------------------------------------------------------------------
# citation_grounding (spec §4.4 + §2.8)
# ---------------------------------------------------------------------------


# Reference keys we recursively harvest from ``artifact.content``. Keeping this
# as a tuple (not a set) so ordering of scan is stable across runs, which makes
# the resulting issue-list deterministic for tests.
_BLOCK_REF_KEYS: tuple[str, ...] = ("source_block_ids",)
_PAGE_REF_KEYS: tuple[str, ...] = ("source_page_ids",)
_SEGMENT_REF_KEYS: tuple[str, ...] = ("source_segment_ids",)


def _verify_citation_grounding(artifact: Artifact, session: Session) -> VerificationResult:
    """Spec §4.4: every citation in the artifact resolves to a Block in the corpus.

    Steps:
      1. Collect every block id referenced in ``artifact.content`` (walking
         nested dicts/lists recursively looking for ``source_block_ids`` keys)
         PLUS every block-kind entry in the top-level ``artifact.sources``.
      2. For each content-cited block id, check it is also declared in
         ``artifact.sources``. If not, emit ``error``.
      3. For each block id (whether cited in content or declared in sources),
         check a ``Block`` row with that id actually exists in the DB. If
         not, emit ``error`` with the id in ``context``.
      4. Page / transcript-segment references are future-proofed: we harvest
         them too so later phases can wire in their own existence checks.
    """
    result = VerificationResult(
        profile="citation_grounding",
        artifact_id=artifact.id,
    )

    # Step 1: parse declared top-level sources.
    declared_block_ids: set[str] = set()
    declared_page_ids: set[str] = set()
    declared_segment_ids: set[str] = set()
    for src in artifact.sources or []:
        kind = src.get("kind")
        sid = src.get("id")
        if not kind or not sid:
            continue
        if kind == "block":
            declared_block_ids.add(sid)
        elif kind == "page":
            declared_page_ids.add(sid)
        elif kind == "transcript_segment":
            declared_segment_ids.add(sid)

    # Step 2: harvest every id referenced inside content. We harvest page /
    # transcript_segment refs too so future phases can wire in their own
    # existence checks; for Phase 2 the block ids are the only ones checked
    # against the declared sources list below.
    cited_block_ids = set(_walk_for_ids(artifact.content, _BLOCK_REF_KEYS))
    _ = set(_walk_for_ids(artifact.content, _PAGE_REF_KEYS))
    _ = set(_walk_for_ids(artifact.content, _SEGMENT_REF_KEYS))

    # Step 3: check every content citation is also declared in sources.
    for bid in sorted(cited_block_ids):
        if bid not in declared_block_ids:
            result.add(
                VerificationIssue(
                    severity="error",
                    profile="citation_grounding",
                    message=(
                        f"Content cites block {bid} but it's not in the "
                        f"artifact.sources list."
                    ),
                    context={"missing_block_id": bid, "location": "sources"},
                )
            )

    # (Page/segment symmetry left intentionally minimal for Phase 2 — §3.11
    # allows page and transcript_segment sources, but the only artifact type
    # shipping in Phase 2 is case_brief which uses block-level refs.)

    # Step 4: check every block id we saw (cited OR declared) exists in DB.
    all_block_ids = declared_block_ids | cited_block_ids
    if all_block_ids:
        present_ids = _fetch_present_block_ids(session, all_block_ids)
        missing = all_block_ids - present_ids
        for bid in sorted(missing):
            result.add(
                VerificationIssue(
                    severity="error",
                    profile="citation_grounding",
                    message=(
                        f"Block id {bid} not found in any book in this corpus."
                    ),
                    context={"missing_block_id": bid, "location": "database"},
                )
            )

    return result


def _fetch_present_block_ids(session: Session, block_ids: Iterable[str]) -> set[str]:
    """Return the subset of ``block_ids`` that exist as ``Block`` rows.

    Uses a single ``WHERE id IN (...)`` query — the set intersection tells us
    which ids are missing. Empty input returns an empty set without hitting DB.

    Every block row has a non-null ``book_id`` foreign key (the Block model
    enforces it), so "exists in DB" = "belongs to some Book in the corpus",
    satisfying spec §4.4's "resolves to a Block in the corpus."
    """
    ids = list(block_ids)
    if not ids:
        return set()
    rows = session.exec(select(Block.id).where(Block.id.in_(ids))).all()
    return set(rows)


def _walk_for_ids(
    node: Any, keys: tuple[str, ...]
) -> Iterator[str]:
    """Recursively walk dict/list structures yielding string ids stored under
    any of ``keys`` (values expected to be lists of strings) OR under a nested
    top-level ``sources`` list of ``{"kind": "block", "id": ...}`` dicts.

    Tolerant of mixed content — skips non-string ids and unknown shapes
    silently rather than raising, because `content` is untyped JSON and a
    half-formed artifact shouldn't crash the verifier.
    """
    if isinstance(node, dict):
        for key in keys:
            if key in node:
                value = node[key]
                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, str):
                            yield item
        # Inline ``sources`` sublists (same shape as top-level).
        if "sources" in node and isinstance(node["sources"], list):
            wanted_kind = _kind_for_keys(keys)
            if wanted_kind is not None:
                for src in node["sources"]:
                    if (
                        isinstance(src, dict)
                        and src.get("kind") == wanted_kind
                        and isinstance(src.get("id"), str)
                    ):
                        yield src["id"]
        # Recurse into every value (including the list at ``keys`` itself —
        # harmless because its strings aren't dict/list themselves).
        for value in node.values():
            yield from _walk_for_ids(value, keys)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_for_ids(item, keys)
    # Scalars terminate the walk.


def _kind_for_keys(keys: tuple[str, ...]) -> str | None:
    """Map the plural ``source_*_ids`` key set to the singular ``"kind"`` used
    in ``sources`` dicts. Keeps ``_walk_for_ids`` generic across block / page /
    segment references."""
    if keys == _BLOCK_REF_KEYS:
        return "block"
    if keys == _PAGE_REF_KEYS:
        return "page"
    if keys == _SEGMENT_REF_KEYS:
        return "transcript_segment"
    return None


# ---------------------------------------------------------------------------
# rule_fidelity (spec §4.4 + §5.2)
# ---------------------------------------------------------------------------


# Legal-document stopword list. Intentionally small — we want the overlap
# metric to remain dominated by substantive nouns/verbs ("covenant",
# "enforceable", "regulatory", "taking") rather than being artificially
# inflated by filler words. List covers:
#   - English function words (the/a/of/to/for/in/…)
#   - Copula verbs (is/are/was/were/be/…)
#   - Demonstratives/possessives (this/that/these/those/it/its)
#   - Frequently-present legal party terms that don't carry doctrinal meaning
#     (court/plaintiff/defendant/case) — matching the prompt's hint.
# Anything genuinely ambiguous (e.g., "held" which can be a rule-verb OR a
# dead word) is left IN so it counts toward overlap.
_RULE_STOPWORDS: frozenset[str] = frozenset(
    {
        "the", "a", "an", "of", "to", "for", "in", "on", "at", "with",
        "and", "or", "but", "not", "is", "are", "was", "were", "be",
        "been", "being", "has", "have", "had", "it", "its", "this",
        "that", "these", "those", "court", "plaintiff", "defendant",
        "case",
    }
)

# Fraction of non-stopword rule tokens that must appear in the source text
# for the rule to be considered "grounded." 60% is deliberately loose because
# (a) we're doing plain word overlap, not semantic similarity, and (b) a valid
# paraphrase can legitimately drop ~30–40% of the original vocabulary. Lower
# than this and almost everything passes; higher and clean prose paraphrases
# get flagged.
_RULE_FIDELITY_THRESHOLD: float = 0.60

# Case-insensitive substring that suppresses the overlap check. The authoring
# prompt can emit ``(paraphrase)`` at the head of the rule text to declare
# that the statement is a deliberate restatement rather than a near-quotation.
_PARAPHRASE_MARKER: str = "(paraphrase)"


def _verify_rule_fidelity(artifact: Artifact, session: Session) -> VerificationResult:
    """Spec §4.4 / §5.2: "compare stated rules in a brief against the case
    opinion text; flag material deviations."

    Strategy (rule-based, zero LLM):

      1. Pull the rule's stated text and its ``source_block_ids`` out of the
         artifact's case_brief content.
      2. If ``source_block_ids`` is empty/missing, emit ``error`` — unsourced
         rules are exactly the hallucination mode we're trying to prevent.
      3. If the rule text contains ``(paraphrase)`` (case-insensitive),
         short-circuit to ``passed`` — the author has explicitly declared
         that token-level divergence is intentional.
      4. Otherwise: tokenize the rule text, drop stopwords, and check what
         fraction of the remaining tokens appear in the concatenated markdown
         of the cited source blocks. Below-threshold emits a ``warning``
         (not an error) because a good paraphrase can legitimately be below
         60% — this surfaces for human review per spec §4.4's soft-warning
         branch.
    """
    result = VerificationResult(
        profile="rule_fidelity",
        artifact_id=artifact.id,
    )

    rule = (artifact.content or {}).get("rule")
    if not isinstance(rule, dict):
        result.add(
            VerificationIssue(
                severity="error",
                profile="rule_fidelity",
                message="Artifact has no rule object in content to verify.",
                context={"missing": "content.rule"},
            )
        )
        return result

    rule_text = rule.get("text", "") or ""
    source_block_ids = rule.get("source_block_ids") or []
    if not isinstance(source_block_ids, list):
        source_block_ids = []

    # Step 2: unsourced rule is a hallucination risk — hard error.
    if not source_block_ids:
        result.add(
            VerificationIssue(
                severity="error",
                profile="rule_fidelity",
                message="Rule has no source attribution.",
                context={"rule_text": rule_text[:200]},
            )
        )
        return result

    # Step 3: explicit paraphrase marker suppresses the overlap check.
    if _PARAPHRASE_MARKER in rule_text.lower():
        return result

    # Step 4: token overlap.
    source_markdown = _concat_source_block_text(session, source_block_ids)
    rule_tokens = _tokenize_non_stopwords(rule_text)

    # An empty rule body with non-empty source list is suspect but we can't
    # compute a ratio — treat it as a warning with a clear message.
    if not rule_tokens:
        result.add(
            VerificationIssue(
                severity="warning",
                profile="rule_fidelity",
                message="Stated rule is empty after stopword removal; cannot verify fidelity.",
                context={"rule_text": rule_text[:200]},
            )
        )
        return result

    source_tokens = set(_tokenize_non_stopwords(source_markdown))
    present = sum(1 for tok in rule_tokens if tok in source_tokens)
    coverage = present / len(rule_tokens)

    if coverage < _RULE_FIDELITY_THRESHOLD:
        pct = round(coverage * 100)
        result.add(
            VerificationIssue(
                severity="warning",
                profile="rule_fidelity",
                message=(
                    f"Stated rule diverges substantially from source: "
                    f"coverage {pct}%."
                ),
                context={
                    "coverage": coverage,
                    "threshold": _RULE_FIDELITY_THRESHOLD,
                    "rule_tokens_checked": len(rule_tokens),
                    "rule_tokens_present": present,
                },
            )
        )

    return result


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize_non_stopwords(text: str) -> list[str]:
    """Lowercase, whitespace-agnostic tokenization that drops stopwords.

    Uses a simple ``[a-z0-9]+`` regex (after lowercasing) so punctuation
    and smart quotes don't create spurious tokens. Returns a list (not a set)
    so the caller can compute an accurate coverage ratio when a token
    repeats in the rule text — each occurrence is counted against the check.
    """
    return [
        tok
        for tok in _TOKEN_RE.findall(text.lower())
        if tok not in _RULE_STOPWORDS
    ]


def _concat_source_block_text(session: Session, block_ids: list[str]) -> str:
    """Fetch the markdown of the cited source blocks and concatenate.

    Missing blocks silently contribute empty text here — the citation-grounding
    profile is the one that flags nonexistent block ids. rule_fidelity cares
    only about overlap against what's actually in the DB.
    """
    if not block_ids:
        return ""
    rows = session.exec(select(Block).where(Block.id.in_(block_ids))).all()
    return "\n\n".join(b.markdown for b in rows)


# ---------------------------------------------------------------------------
# rubric_coverage (spec §4.4 + §5.5 Path A step 5)
# ---------------------------------------------------------------------------


# The three kinds of rubric items that a Grade must score. Ordered for stable
# iteration (and therefore deterministic test output). The string values match
# ``grade.json`` ``rubric_item_kind`` enum exactly so we can compare without
# translation.
_RUBRIC_SECTIONS: tuple[tuple[str, str], ...] = (
    ("required_issues", "required_issue"),
    ("required_rules", "required_rule"),
    ("expected_counterarguments", "expected_counterargument"),
)


def _verify_rubric_coverage(artifact: Artifact, session: Session) -> VerificationResult:
    """Spec §4.4 + §5.5 Path A step 5: "for IRAC grading, verify every rubric
    item was actually scored."

    Strategy (rule-based, zero LLM):

      1. Require the input artifact to be ``ArtifactType.GRADE``. Passing a
         different kind of artifact is a programmer error; emit a hard error
         issue rather than raising so the caller gets a structured result.
      2. Load the Rubric artifact referenced by ``content["rubric_id"]``. If
         the id is missing or the row isn't actually a ``RUBRIC`` artifact,
         emit ``error`` and return early — nothing else is checkable.
      3. Build the expected set: every ``rubric.required_issues[*].id``,
         every ``rubric.required_rules[*].id``, every
         ``rubric.expected_counterarguments[*].id`` — tagged with the kind
         string ``grade.json`` uses (``required_issue`` / ``required_rule`` /
         ``expected_counterargument``). For each expected id, check that a
         ``per_rubric_scores`` entry with matching ``rubric_item_id`` exists.
         Missing → ``error``.
      4. For every scored item, cross-check: (a) the ``rubric_item_kind`` the
         grade recorded matches the section the rubric actually lists the id
         under (mismatch → ``warning``), (b) ``points_possible > 0`` (non-
         positive → ``warning``).
      5. Unknown ``rubric_item_id`` values in the grade (don't match any
         section of the rubric) are flagged as warnings — the main spec is
         "every rubric item was scored," extra scores are less concerning
         but still worth surfacing.
    """
    result = VerificationResult(
        profile="rubric_coverage",
        artifact_id=artifact.id,
    )

    # Step 1: type gate.
    if artifact.type is not ArtifactType.GRADE:
        result.add(
            VerificationIssue(
                severity="error",
                profile="rubric_coverage",
                message=(
                    f"rubric_coverage expects a Grade artifact; got "
                    f"artifact.type={artifact.type.value!r}."
                ),
                context={"artifact_type": artifact.type.value},
            )
        )
        return result

    content = artifact.content or {}

    # Step 2: rubric_id resolution.
    rubric_id = content.get("rubric_id")
    if not rubric_id or not isinstance(rubric_id, str):
        result.add(
            VerificationIssue(
                severity="error",
                profile="rubric_coverage",
                message="Grade artifact has no rubric_id in content.",
                context={"missing": "content.rubric_id"},
            )
        )
        return result

    rubric_artifact = session.exec(
        select(Artifact).where(Artifact.id == rubric_id)
    ).first()
    if rubric_artifact is None:
        result.add(
            VerificationIssue(
                severity="error",
                profile="rubric_coverage",
                message=(
                    f"Rubric artifact {rubric_id} referenced by grade was "
                    f"not found."
                ),
                context={"missing_rubric_id": rubric_id},
            )
        )
        return result

    if rubric_artifact.type is not ArtifactType.RUBRIC:
        result.add(
            VerificationIssue(
                severity="error",
                profile="rubric_coverage",
                message=(
                    f"Artifact {rubric_id} referenced by grade is not a "
                    f"rubric (type={rubric_artifact.type.value})."
                ),
                context={
                    "rubric_id": rubric_id,
                    "actual_type": rubric_artifact.type.value,
                },
            )
        )
        return result

    rubric_content = rubric_artifact.content or {}

    # Step 3: build {rubric_item_id -> expected kind} map and check scored-ness.
    expected_items: dict[str, str] = {}
    for section_key, kind in _RUBRIC_SECTIONS:
        items = rubric_content.get(section_key) or []
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            item_id = item.get("id")
            if isinstance(item_id, str) and item_id:
                # First registration wins — if the same id appears in multiple
                # sections (shouldn't happen per schema but defensive),
                # subsequent sections get ignored for the kind-of-record.
                expected_items.setdefault(item_id, kind)

    scores = content.get("per_rubric_scores") or []
    if not isinstance(scores, list):
        scores = []

    scored_by_id: dict[str, dict[str, Any]] = {}
    for score in scores:
        if not isinstance(score, dict):
            continue
        score_id = score.get("rubric_item_id")
        if isinstance(score_id, str) and score_id:
            scored_by_id.setdefault(score_id, score)

    for item_id, expected_kind in expected_items.items():
        if item_id not in scored_by_id:
            result.add(
                VerificationIssue(
                    severity="error",
                    profile="rubric_coverage",
                    message=(
                        f"Rubric item {item_id} ({expected_kind}) was not "
                        f"scored."
                    ),
                    context={
                        "missing_rubric_item_id": item_id,
                        "rubric_item_kind": expected_kind,
                    },
                )
            )

    # Step 4: scored-item sanity checks.
    for score_id, score in scored_by_id.items():
        expected_kind = expected_items.get(score_id)
        recorded_kind = score.get("rubric_item_kind")

        if expected_kind is None:
            # Grade scored an id the rubric doesn't mention. Not a hard error
            # (maybe a renamed item), but surface it.
            result.add(
                VerificationIssue(
                    severity="warning",
                    profile="rubric_coverage",
                    message=(
                        f"Scored rubric item {score_id} is not present in "
                        f"the rubric."
                    ),
                    context={
                        "rubric_item_id": score_id,
                        "recorded_kind": (
                            recorded_kind if isinstance(recorded_kind, str) else None
                        ),
                    },
                )
            )
        elif isinstance(recorded_kind, str) and recorded_kind != expected_kind:
            result.add(
                VerificationIssue(
                    severity="warning",
                    profile="rubric_coverage",
                    message=(
                        f"Grade scored {score_id} as {recorded_kind!r} but "
                        f"the rubric has it under {expected_kind!r}."
                    ),
                    context={
                        "rubric_item_id": score_id,
                        "recorded_kind": recorded_kind,
                        "expected_kind": expected_kind,
                    },
                )
            )

        points_possible = score.get("points_possible")
        if isinstance(points_possible, (int, float)) and points_possible <= 0:
            result.add(
                VerificationIssue(
                    severity="warning",
                    profile="rubric_coverage",
                    message=(
                        f"Rubric item {score_id} has non-positive "
                        f"points_possible={points_possible}."
                    ),
                    context={
                        "rubric_item_id": score_id,
                        "points_possible": points_possible,
                    },
                )
            )

    return result


# ---------------------------------------------------------------------------
# Phase 3 stubs
# ---------------------------------------------------------------------------


_MIN_REQUIRED_ISSUES_COUNT = 3


def _issue_spotting_completeness_rule_based(
    artifact: Artifact,
) -> VerificationResult:
    """Spec §5.5 Path B step 3 — rule-based Phase 3 implementation.

    The full LLM-backed version (a second pass that re-spots issues from
    scratch and compares against the rubric, augmenting on miss) is logged
    in SPEC_QUESTIONS.md as a follow-up. This rule-based version catches
    the obvious rubric-quality failure modes that would have made the LLM
    version fire anyway:

    - Rubric weights should sum to ~1.0 (±0.05 tolerance). Otherwise the
      grader's weighted score is miscalibrated.
    - `required_issues` should have at least 3 entries — a single-issue
      rubric is almost always a generation miss for an exam hypo.
    - Each required_issue needs a non-empty `label`.
    - `prompt_role` should be set (wrong voice = lost points per Pollack).
    - `anti_patterns` should be non-empty when the hypo was generated from
      a professor profile — warning, not error.

    Tolerates either shape: a HYPO artifact (rubric lives under
    `content["rubric"]`) or a RUBRIC artifact (rubric IS the content).
    """
    result = VerificationResult(
        profile="issue_spotting_completeness",
        artifact_id=artifact.id,
        issues=[],
        passed=True,
        soft_warnings=[],
    )

    if artifact.type == ArtifactType.HYPO:
        rubric = artifact.content.get("rubric")
        if not isinstance(rubric, dict):
            result.add(
                VerificationIssue(
                    severity="error",
                    profile="issue_spotting_completeness",
                    message="HYPO artifact missing embedded 'rubric' dict in content.",
                    context={"artifact_type": artifact.type.value},
                )
            )
            return result
    elif artifact.type == ArtifactType.RUBRIC:
        rubric = artifact.content
    else:
        result.add(
            VerificationIssue(
                severity="error",
                profile="issue_spotting_completeness",
                message=(
                    f"issue_spotting_completeness expects a HYPO or RUBRIC artifact; "
                    f"got {artifact.type.value}."
                ),
                context={"artifact_type": artifact.type.value},
            )
        )
        return result

    required_issues = rubric.get("required_issues") or []

    if len(required_issues) < _MIN_REQUIRED_ISSUES_COUNT:
        result.add(
            VerificationIssue(
                severity="warning",
                profile="issue_spotting_completeness",
                message=(
                    f"Only {len(required_issues)} required_issues; exam rubrics "
                    f"typically have {_MIN_REQUIRED_ISSUES_COUNT}+. The generator "
                    "may have under-covered the hypo's issue density."
                ),
                context={"issue_count": len(required_issues)},
            )
        )

    weights_total = sum(float(i.get("weight", 0)) for i in required_issues)
    if required_issues and not (0.95 <= weights_total <= 1.05):
        result.add(
            VerificationIssue(
                severity="warning",
                profile="issue_spotting_completeness",
                message=(
                    f"required_issues weights sum to {weights_total:.3f}, "
                    "expected ~1.0 (±0.05). Grade scaling may be off."
                ),
                context={"weights_total": weights_total},
            )
        )

    for i, issue in enumerate(required_issues):
        if not str(issue.get("label", "")).strip():
            result.add(
                VerificationIssue(
                    severity="error",
                    profile="issue_spotting_completeness",
                    message=(
                        f"required_issues[{i}] has empty or missing label — "
                        "grader cannot tie scores back to a named issue."
                    ),
                    context={"index": i, "issue_id": issue.get("id", "")},
                )
            )

    if not str(rubric.get("prompt_role", "")).strip():
        result.add(
            VerificationIssue(
                severity="warning",
                profile="issue_spotting_completeness",
                message=(
                    "rubric.prompt_role is empty — voice-conformance checks "
                    "(spec Appendix A: 'wrong voice = lost points') can't run."
                ),
                context={},
            )
        )

    if not rubric.get("anti_patterns"):
        result.add(
            VerificationIssue(
                severity="warning",
                profile="issue_spotting_completeness",
                message=(
                    "rubric.anti_patterns is empty. If the hypo was generated "
                    "with a professor profile, pet peeves should have been "
                    "copied into anti_patterns — grader will miss them."
                ),
                context={},
            )
        )

    return result


# ---------------------------------------------------------------------------
# Public module surface
# ---------------------------------------------------------------------------


__all__ = [
    "KNOWN_PROFILES",
    "VerificationIssue",
    "VerificationResult",
    "verify",
]
