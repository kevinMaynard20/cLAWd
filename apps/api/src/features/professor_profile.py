"""Professor-profile builder + editor (spec §5.13, §3.7).

Orchestrates the LLM extraction pass that turns uploaded exam memos + syllabus
into a structured :class:`ProfessorProfile`. The actual LLM call goes through
the :func:`primitives.generate.generate` primitive so we get:

- Artifact envelope persistence (``ArtifactType.PROFESSOR_PROFILE``).
- Cost tracking (``CostEvent`` emitted via generate).
- Replay-cache short-circuiting on unchanged inputs.
- JSON-schema validation against ``schemas/professor_profile.json``.

Unlike case-brief, the *payload* of the artifact is ALSO the long-lived
ProfessorProfile row — the SQLModel is authoritative because every downstream
generation loads it by ``(corpus_id, professor_name)``. The Artifact row
preserves the extraction-time provenance (prompt template, model, cache key,
cost); the ProfessorProfile row is the user-editable, lookup-friendly copy.

Upsert semantics: one profile per ``(corpus_id, professor_name)`` — rebuilding
with the same pair updates in place and bumps ``updated_at``. Spec §3.7 says
re-extraction is legal when new artifacts arrive; we preserve row identity so
foreign keys from downstream features (rubric extraction, IRAC grading) keep
resolving.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import jsonschema
from sqlalchemy import desc
from sqlmodel import Session, select

from data.models import (
    Artifact,
    ArtifactType,
    CreatedBy,
    ProfessorProfile,
)
from primitives.generate import GenerateError, GenerateRequest, generate
from primitives.prompt_loader import load_output_schema, load_template

# ---------------------------------------------------------------------------
# Public request / response types
# ---------------------------------------------------------------------------


@dataclass
class ProfileBuildRequest:
    """Inputs for :func:`build_profile_from_memos`.

    ``memo_artifact_ids`` points at prior :class:`Artifact` rows (produced by
    :func:`features.past_exam_ingest.ingest_past_exam`) — we pull their
    markdown out of ``content["markdown"]`` and pass them to the extraction
    prompt as ``memo_sources``. Any id that doesn't exist or isn't a past_exam
    / grader_memo is silently skipped; the caller sees this in
    ``ProfileBuildResult.warnings``.
    """

    corpus_id: str
    professor_name: str
    course: str
    school: str | None = None
    memo_artifact_ids: list[str] = field(default_factory=list)
    syllabus_markdown: str | None = None


@dataclass
class ProfileBuildResult:
    profile: ProfessorProfile
    cache_hit: bool
    warnings: list[str] = field(default_factory=list)


class ProfileError(RuntimeError):
    """Raised on unrecoverable profile-builder failures (no usable memos,
    generation error, etc.)."""


# ---------------------------------------------------------------------------
# Appendix A seed data (spec §8, Appendix A)
# ---------------------------------------------------------------------------


#: Pre-built Pollack profile drawn from spec Appendix A. Shape matches
#: ``schemas/professor_profile.json`` so tests and demos can seed a realistic
#: profile without running the LLM extraction pass.
APPENDIX_A_POLLACK_PROFILE: dict[str, Any] = {
    "professor_name": "Pollack",
    "course": "Property",
    "school": "Benjamin N. Cardozo School of Law",
    "exam_format": {
        "duration_hours": 5.0,
        "word_limit": 4000,
        "open_book": False,
        "structure": [
            {
                "part": "I",
                "weight": 10,
                "type": "multiple_choice",
                "count": 10,
            },
            {
                "part": "II-IV",
                "weight": 30,
                "type": "issue_spotter_essay",
                "density": "7-10 distinct issues per fact pattern",
            },
        ],
        "prompt_conventions": [
            (
                "Always ends with: 'If there are any factual ambiguities or "
                "unanswered legal questions that would affect your analysis "
                "of these issues, explain what they are and how they would "
                "affect that analysis.'"
            ),
            (
                "Prompt role varies per part: law clerk memo (neutral), "
                "client's lawyer (advocate), brief (advocate in persona). "
                "Wrong voice = lost points."
            ),
        ],
    },
    "pet_peeves": [
        {
            "name": "hedge_without_resolution",
            "pattern": (
                "'It depends on the jurisdiction' / 'it's ultimately a fact "
                "question' / 'the court would need to evaluate the facts'"
            ),
            "severity": "high",
            "quote": (
                "'Well, Client, it all depends on the facts' is not the kind "
                "of analysis that anyone will pay you very much to provide."
            ),
            "source": "2023 memo p.2, 2024 memo pp.4-5",
        },
        {
            "name": "clearly_as_argument_substitution",
            "pattern": "Using 'clearly' to avoid making an argument",
            "severity": "high",
            "quote": (
                "The word 'clearly' in a brief is a neon sign that a lawyer "
                "has no real argument and probably deserves to lose."
            ),
            "source": "2024 memo p.4",
        },
        {
            "name": "mismatched_future_interests",
            "pattern": (
                "Inventing interests not on the numerus clausus menu; pairing "
                "interests that can't legally coexist (e.g., 'remainder vested "
                "subject to open' + 'contingent remainder')"
            ),
            "severity": "high",
            "quote": None,
            "source": "2023 memo p.3, 2024 memo p.5",
            "must_know_pairings": [
                "contingent remainder ↔ alternate contingent remainder OR reversion",
                "vested subject to open ↔ (nothing — no other future interest)",
                "indefeasibly vested ↔ (nothing)",
                "vested subject to complete divestment ↔ executory interest",
            ],
        },
        {
            "name": "rule_recited_not_applied",
            "pattern": (
                "Stating a rule without tying it to the specific facts of "
                "the hypo"
            ),
            "severity": "high",
            "quote": None,
            "source": (
                "2023 memo p.1 ('legal analysis always requires you to apply "
                "that rule to these facts')"
            ),
        },
        {
            "name": "read_the_prompt",
            "pattern": (
                "Answering 'should X' when asked 'can X'; writing 'Harriet "
                "could argue' when instructed to write as a detached law clerk"
            ),
            "severity": "high",
            "quote": None,
            "source": "2023 memo p.1, 2024 memo p.11",
        },
        {
            "name": "no_arguing_in_the_alternative",
            "pattern": (
                "Committing to one interpretation and offering no backup when "
                "the prompt signaled ambiguity"
            ),
            "severity": "high",
            "quote": "You've got to get used to arguing in the alternative.",
            "source": "2024 memo pp.5-6",
        },
        {
            "name": "ny_adverse_possession_reasonable_basis",
            "pattern": (
                "Conflating 'the claimant thought they owned it' with 'the "
                "claimant had a reasonable basis for thinking they owned it'"
            ),
            "severity": "medium",
            "quote": None,
            "source": (
                "Flagged in both 2023 and 2024 memos as a year-over-year "
                "repeat error"
            ),
        },
        {
            "name": "conclusion_mismatches_analysis",
            "pattern": "'In sum X. Therefore not-X.'",
            "severity": "medium",
            "quote": None,
            "source": "2023 memo p.2",
        },
    ],
    "favored_framings": [
        "Numerus clausus — the menu of estates is closed",
        "Penn Central three-factor balancing as the default for regulatory takings",
        (
            "Per se takings as carve-outs (Loretto physical occupation; "
            "Lucas total wipeout)"
        ),
        (
            "Order of operations: procedural validity before substantive "
            "reasonableness; nuisance determination before remedy"
        ),
    ],
    "stable_traps": [
        {
            "name": "deed_language_FSSEL_vs_FSD",
            "desc": (
                "Durational language ('so long as') in a conveyance to "
                "third-party future-interest holder → FSSEL, not FSD."
            ),
            "source": "2023 memo p.3",
        },
        {
            "name": "shelter_rule_reconstruction",
            "desc": (
                "Shelter Rule does not let you 'mix and match' winning halves "
                "across buyers. Grantee inherits grantor's whole position."
            ),
            "source": "2024 memo p.7",
        },
        {
            "name": "changed_conditions_requires_both_internal_and_external",
            "desc": (
                "Under River Heights, changed-conditions doctrine requires "
                "radical change INSIDE the restricted area as well as outside."
            ),
            "source": "2023 memo p.6",
        },
    ],
    "voice_conventions": [
        {
            "name": "prompt_role_varies",
            "desc": (
                "Voice must match the prompt's assigned role: law clerk memo "
                "→ neutral; client's lawyer → advocate; brief → advocate in "
                "persona. Wrong voice = lost points."
            ),
        },
        {
            "name": "ambiguity_closer",
            "desc": (
                "Every answer must close with a pass at the prompt's standard "
                "closing: 'If there are any factual ambiguities or unanswered "
                "legal questions that would affect your analysis of these "
                "issues, explain what they are and how they would affect that "
                "analysis.' Skipping this = forfeit of easy points."
            ),
        },
    ],
    "commonly_tested": [
        "RAP on executory interests / contingent remainders",
        "Recording acts (race / notice / race-notice distinctions) + Shelter Rule",
        "Covenants running at law vs. in equity",
        "Easement creation methods (express, implied, prescription, estoppel)",
        (
            "Landlord-tenant: assignment vs sublease, Kendall, duty to mitigate, "
            "quiet enjoyment vs habitability"
        ),
        "Takings: Loretto / Lucas / Penn Central",
        "Co-ownership: joint tenancy severance, lien vs title theory",
        "Zoning: variances, nonconforming uses, special exceptions",
    ],
    "source_artifact_paths": [
        "storage/artifacts/pollack_2023_memo.md",
        "storage/artifacts/pollack_2024_memo.md",
        "storage/artifacts/pollack_2025_exam.md",
    ],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_memo_sources(
    session: Session,
    artifact_ids: list[str],
) -> tuple[list[dict[str, str]], list[str]]:
    """Load the Artifact rows backing ``memo_artifact_ids`` and format them for
    the extraction prompt.

    Returns ``(memo_sources, warnings)``. ``memo_sources`` is the list of
    ``{path, markdown}`` dicts the prompt expects; ``warnings`` collects
    per-id problems so callers can surface them in the HTTP response without
    failing the whole build.
    """
    warnings: list[str] = []
    sources: list[dict[str, str]] = []
    allowed_types = {ArtifactType.PAST_EXAM, ArtifactType.GRADER_MEMO}

    for art_id in artifact_ids:
        row = session.exec(select(Artifact).where(Artifact.id == art_id)).first()
        if row is None:
            warnings.append(f"artifact {art_id} not found; skipping")
            continue
        if row.type not in allowed_types:
            warnings.append(
                f"artifact {art_id} is type={row.type.value}; "
                "expected past_exam or grader_memo; skipping"
            )
            continue
        markdown = row.content.get("markdown") if isinstance(row.content, dict) else None
        if not markdown:
            warnings.append(f"artifact {art_id} has no markdown; skipping")
            continue
        # Provenance path: prefer user-supplied source_paths; fall back to the
        # artifact id so the profile has something to cite.
        raw_paths = row.content.get("source_paths") if isinstance(row.content, dict) else None
        if isinstance(raw_paths, list) and raw_paths:
            path = str(raw_paths[0])
        else:
            path = f"artifact://{row.id}"
        sources.append({"path": path, "markdown": str(markdown)})

    return sources, warnings


def _source_artifact_paths(
    memo_sources: list[dict[str, str]],
    syllabus_markdown: str | None,
) -> list[str]:
    """Collect the paths we handed the prompt; stored on the profile so later
    re-extraction knows which inputs were already consumed."""
    paths = [s["path"] for s in memo_sources]
    if syllabus_markdown is not None:
        paths.append("syllabus://inline")
    return paths


def _validate_against_schema(payload: dict[str, Any]) -> None:
    """Raise ``ValueError`` when ``payload`` doesn't match the professor-profile
    JSON schema. Used by :func:`update_profile` — we load the template + schema
    once per call, which is cheap given the template cache upstream.
    """
    template = load_template("professor_profile_extraction")
    schema = load_output_schema(template)
    try:
        jsonschema.validate(payload, schema)
    except jsonschema.ValidationError as exc:
        path = "/".join(str(p) for p in exc.absolute_path) or "(root)"
        raise ValueError(f"profile payload failed schema at {path}: {exc.message}") from exc


# ---------------------------------------------------------------------------
# Build / upsert
# ---------------------------------------------------------------------------


def build_profile_from_memos(
    session: Session,
    req: ProfileBuildRequest,
) -> ProfileBuildResult:
    """Run the extraction prompt against the provided memos/syllabus and
    persist both the Artifact envelope and the ProfessorProfile row.

    Raises :class:`ProfileError` on unrecoverable failures (no usable memos,
    generate-primitive error). Soft problems (individual memo id not found)
    flow through ``ProfileBuildResult.warnings``.
    """
    memo_sources, warnings = _load_memo_sources(session, req.memo_artifact_ids)
    if not memo_sources and req.syllabus_markdown is None:
        raise ProfileError(
            "No usable memo artifacts or syllabus markdown provided. "
            "Upload a past exam via POST /ingest/past-exam first."
        )

    inputs: dict[str, Any] = {
        "professor_name": req.professor_name,
        "course": req.course,
        "school": req.school,
        "memo_sources": memo_sources,
        "syllabus_markdown": req.syllabus_markdown,
    }

    try:
        result = generate(
            GenerateRequest(
                template_name="professor_profile_extraction",
                inputs=inputs,
                artifact_type=ArtifactType.PROFESSOR_PROFILE,
                corpus_id=req.corpus_id,
            )
        )
    except GenerateError as exc:
        raise ProfileError(f"professor-profile extraction failed: {exc}") from exc

    payload = dict(result.artifact.content)

    # The schema allows source_artifact_paths to be overridden by the model,
    # but we always want a superset that includes the paths we actually fed
    # in — otherwise re-extraction can't tell which memos were consumed.
    provenance = _source_artifact_paths(memo_sources, req.syllabus_markdown)
    existing_paths = payload.get("source_artifact_paths") or []
    merged_paths = list(dict.fromkeys([*existing_paths, *provenance]))
    payload["source_artifact_paths"] = merged_paths

    profile = _upsert_profile(session, req, payload)
    return ProfileBuildResult(
        profile=profile,
        cache_hit=result.cache_hit,
        warnings=warnings,
    )


def _upsert_profile(
    session: Session,
    req: ProfileBuildRequest,
    payload: dict[str, Any],
) -> ProfessorProfile:
    """Insert-or-update the ProfessorProfile row for (corpus_id, professor_name).

    The uniqueness index on that pair keeps us honest: if two concurrent
    builds race, the second commit fails and the caller retries — but we
    commit inside the FastAPI-request session so in practice there's no
    concurrency window.
    """
    existing = session.exec(
        select(ProfessorProfile)
        .where(ProfessorProfile.corpus_id == req.corpus_id)
        .where(ProfessorProfile.professor_name == req.professor_name)
    ).first()

    now = datetime.now(tz=UTC)

    if existing is None:
        profile = ProfessorProfile(
            corpus_id=req.corpus_id,
            professor_name=req.professor_name,
            course=req.course,
            school=req.school,
            exam_format=payload.get("exam_format", {}),
            pet_peeves=payload.get("pet_peeves", []),
            favored_framings=payload.get("favored_framings", []),
            stable_traps=payload.get("stable_traps", []),
            voice_conventions=payload.get("voice_conventions", []),
            commonly_tested=payload.get("commonly_tested", []),
            source_artifact_paths=payload.get("source_artifact_paths", []),
        )
        session.add(profile)
    else:
        existing.course = req.course
        existing.school = req.school if req.school is not None else existing.school
        existing.exam_format = payload.get("exam_format", existing.exam_format)
        existing.pet_peeves = payload.get("pet_peeves", existing.pet_peeves)
        existing.favored_framings = payload.get(
            "favored_framings", existing.favored_framings
        )
        existing.stable_traps = payload.get("stable_traps", existing.stable_traps)
        existing.voice_conventions = payload.get(
            "voice_conventions", existing.voice_conventions
        )
        existing.commonly_tested = payload.get(
            "commonly_tested", existing.commonly_tested
        )
        existing.source_artifact_paths = payload.get(
            "source_artifact_paths", existing.source_artifact_paths
        )
        existing.updated_at = now
        session.add(existing)
        profile = existing

    session.commit()
    session.refresh(profile)
    session.expunge(profile)
    return profile


# ---------------------------------------------------------------------------
# Update / load
# ---------------------------------------------------------------------------


# Fields a caller may edit through PATCH; keep the mapping explicit so we don't
# accidentally let the UI rewrite corpus_id or created_at.
_EDITABLE_FIELDS: frozenset[str] = frozenset(
    {
        "course",
        "school",
        "exam_format",
        "pet_peeves",
        "favored_framings",
        "stable_traps",
        "voice_conventions",
        "commonly_tested",
        "source_artifact_paths",
    }
)


def update_profile(
    session: Session,
    profile_id: str,
    edits: dict[str, Any],
) -> ProfessorProfile:
    """Apply user edits to an existing ProfessorProfile row.

    We assemble the profile's post-edit state as a JSON payload and validate
    against the professor_profile schema before persisting — the structured
    editor UI (§5.13) should produce valid payloads, but a misbehaving client
    must not be able to break a downstream grader by writing garbage here.

    Raises :class:`ValueError` on unknown edit fields or schema failures, and
    :class:`LookupError` when the profile id doesn't exist.
    """
    profile = session.exec(
        select(ProfessorProfile).where(ProfessorProfile.id == profile_id)
    ).first()
    if profile is None:
        raise LookupError(f"profile {profile_id} not found")

    unknown = set(edits) - _EDITABLE_FIELDS
    if unknown:
        raise ValueError(
            f"cannot edit protected fields: {sorted(unknown)}. "
            f"Editable: {sorted(_EDITABLE_FIELDS)}"
        )

    merged: dict[str, Any] = {
        "professor_name": profile.professor_name,
        "course": edits.get("course", profile.course),
        "school": edits.get("school", profile.school),
        "exam_format": edits.get("exam_format", profile.exam_format),
        "pet_peeves": edits.get("pet_peeves", profile.pet_peeves),
        "favored_framings": edits.get("favored_framings", profile.favored_framings),
        "stable_traps": edits.get("stable_traps", profile.stable_traps),
        "voice_conventions": edits.get("voice_conventions", profile.voice_conventions),
        "commonly_tested": edits.get("commonly_tested", profile.commonly_tested),
        "source_artifact_paths": edits.get(
            "source_artifact_paths", profile.source_artifact_paths
        ),
    }

    _validate_against_schema(merged)

    for key in _EDITABLE_FIELDS:
        if key in edits:
            setattr(profile, key, edits[key])
    profile.updated_at = datetime.now(tz=UTC)
    session.add(profile)
    session.commit()
    session.refresh(profile)
    session.expunge(profile)
    return profile


def load_profile_for_corpus(
    session: Session,
    corpus_id: str,
    professor_name: str | None = None,
) -> ProfessorProfile | None:
    """Look up the ProfessorProfile for a corpus.

    With ``professor_name`` supplied: returns the exact match or None.

    Without it: returns the single profile in the corpus if exactly one
    exists; otherwise returns None. This matches the common case where a
    corpus is one course = one professor — callers don't need to remember
    the name.
    """
    stmt = select(ProfessorProfile).where(ProfessorProfile.corpus_id == corpus_id)
    if professor_name is not None:
        stmt = stmt.where(ProfessorProfile.professor_name == professor_name)

    rows = session.exec(stmt).all()
    if not rows:
        return None
    if professor_name is not None:
        found = rows[0]
        session.expunge(found)
        return found
    if len(rows) == 1:
        session.expunge(rows[0])
        return rows[0]
    # Ambiguous: multiple profiles in the corpus and no name supplied.
    return None


def list_profiles_for_corpus(
    session: Session,
    corpus_id: str,
    professor_name: str | None = None,
) -> list[ProfessorProfile]:
    """Return all profiles matching the filter. Route uses this to back the
    ``GET /profiles`` index endpoint."""
    stmt = select(ProfessorProfile).where(ProfessorProfile.corpus_id == corpus_id)
    if professor_name is not None:
        stmt = stmt.where(ProfessorProfile.professor_name == professor_name)
    stmt = stmt.order_by(desc(ProfessorProfile.updated_at))
    rows = list(session.exec(stmt).all())
    for row in rows:
        session.expunge(row)
    return rows


def get_profile(session: Session, profile_id: str) -> ProfessorProfile | None:
    """Fetch a profile by id. Returns None when absent."""
    found = session.exec(
        select(ProfessorProfile).where(ProfessorProfile.id == profile_id)
    ).first()
    if found is None:
        return None
    session.expunge(found)
    return found


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def seed_pollack_profile(session: Session, corpus_id: str) -> ProfessorProfile:
    """Seed or return the Appendix A Pollack profile for ``corpus_id``.

    Idempotent: calling twice returns the existing row the second time. Tests
    and demos can skip the LLM extraction step entirely by calling this. A
    matching Artifact row is also created so the cost-attribution UI has
    something to render; its ``prompt_template`` is flagged as ``seed`` so
    downstream consumers can tell it apart from a real extraction.
    """
    existing = session.exec(
        select(ProfessorProfile)
        .where(ProfessorProfile.corpus_id == corpus_id)
        .where(ProfessorProfile.professor_name == "Pollack")
    ).first()
    if existing is not None:
        session.expunge(existing)
        return existing

    payload = APPENDIX_A_POLLACK_PROFILE

    # Create the accompanying Artifact so the profile has provenance. Tagged
    # created_by=USER because the Pollack profile is sourced from spec's
    # Appendix A (human-authored), not an LLM run.
    from decimal import Decimal

    artifact = Artifact(
        corpus_id=corpus_id,
        type=ArtifactType.PROFESSOR_PROFILE,
        created_by=CreatedBy.USER,
        sources=[],
        content=dict(payload),
        prompt_template="seed:appendix_a_pollack@1.0.0",
        llm_model="",
        cost_usd=Decimal("0"),
        cache_key="",
        regenerable=False,
    )
    session.add(artifact)

    profile = ProfessorProfile(
        corpus_id=corpus_id,
        professor_name=payload["professor_name"],
        course=payload["course"],
        school=payload.get("school"),
        exam_format=payload["exam_format"],
        pet_peeves=payload["pet_peeves"],
        favored_framings=payload["favored_framings"],
        stable_traps=payload["stable_traps"],
        voice_conventions=payload["voice_conventions"],
        commonly_tested=payload["commonly_tested"],
        source_artifact_paths=payload["source_artifact_paths"],
    )
    session.add(profile)
    session.commit()
    session.refresh(profile)
    session.expunge(profile)
    return profile


__all__ = [
    "APPENDIX_A_POLLACK_PROFILE",
    "ProfileBuildRequest",
    "ProfileBuildResult",
    "ProfileError",
    "build_profile_from_memos",
    "get_profile",
    "list_profiles_for_corpus",
    "load_profile_for_corpus",
    "seed_pollack_profile",
    "update_profile",
]
