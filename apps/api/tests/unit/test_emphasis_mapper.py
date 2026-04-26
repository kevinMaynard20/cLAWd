"""Unit tests for :mod:`features.emphasis_mapper` (spec §5.7).

Covers:
- Per-subject feature extraction from TranscriptSegment data
  (minutes-on, return counts, hypotheticals, disclaimed propagation,
  engaged-question counting).
- Mechanical scoring formula — disclaimed penalizes, extreme inputs are
  clamped, disclaimed score < identical non-disclaimed score.
- Orchestration happy path with fake Anthropic client: persists
  EmphasisItem rows, ranked by exam_signal_score DESC.
- Cache-hit semantics (second call without force_regenerate returns
  cache_hit=True).
- force_regenerate=True path replaces items and calls the LLM again.
- Missing-transcript -> EmphasisMapError.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import Session, select

from costs import tracker as tracker_mod
from costs.emphasis_weights import (
    EmphasisWeights,
    reset_weights,
)
from data import db
from data.models import (
    Corpus,
    CostEvent,
    EmphasisItem,
    EmphasisSubjectKind,
    Speaker,
    Transcript,
    TranscriptSegment,
    TranscriptSourceType,
)
from features import emphasis_mapper
from features.emphasis_mapper import (
    EmphasisMapError,
    EmphasisMapRequest,
    EmphasisSubjectFeatures,
    build_emphasis_map,
    compute_provisional_score,
    compute_subject_features,
)

# ---------------------------------------------------------------------------
# Fake Anthropic client (mirrors the anthropic.Anthropic surface we use)
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
    def __init__(self, payload_fn) -> None:
        self._payload_fn = payload_fn
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _FakeResponse:
        self.calls.append(kwargs)
        payload = self._payload_fn(len(self.calls))
        return _FakeResponse(
            content=[_FakeTextContent(text=payload)],
            usage=_FakeUsage(input_tokens=1500, output_tokens=900),
        )


class _FakeClient:
    def __init__(self, payload_fn) -> None:
        self.messages = _FakeMessages(payload_fn)


def _fake_factory(payload_fn):
    holder: dict[str, _FakeClient] = {}

    def _factory(_api_key: str) -> _FakeClient:
        if "client" not in holder:
            holder["client"] = _FakeClient(payload_fn)
        return holder["client"]

    _factory.holder = holder  # type: ignore[attr-defined]
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

    from credentials import keyring_backend

    tracker_mod.reset_session_id()
    reset_weights()
    keyring_backend.store_anthropic_key("sk-ant-test-FAKEKEY-1234567890-LAST")

    yield
    emphasis_mapper.set_anthropic_client_factory(None)
    reset_weights()
    db.reset_engine()


def _seed_transcript(
    session: Session,
    *,
    corpus_id: str | None = None,
    cleaned_text: str = "Lecture body about Shelley v. Kraemer and state action.",
    topic: str = "State action doctrine",
) -> str:
    """Persist a Corpus + Transcript and return the transcript id."""
    if corpus_id is None:
        corpus = Corpus(name="Property – Pollack", course="Property")
        session.add(corpus)
        session.commit()
        session.refresh(corpus)
        corpus_id = corpus.id

    transcript = Transcript(
        id="tx_" + "a" * 60,
        corpus_id=corpus_id,
        source_type=TranscriptSourceType.TEXT,
        source_path="/uploads/lec.txt",
        topic=topic,
        raw_text=cleaned_text,
        cleaned_text=cleaned_text,
    )
    session.add(transcript)
    session.commit()
    session.refresh(transcript)
    return transcript.id


def _segment(
    transcript_id: str,
    *,
    order_index: int,
    start_char: int,
    end_char: int,
    content: str = "segment content",
    speaker: Speaker = Speaker.PROFESSOR,
    mentioned_cases: list[str] | None = None,
    mentioned_rules: list[str] | None = None,
    mentioned_concepts: list[str] | None = None,
    sentiment_flags: list[str] | None = None,
) -> TranscriptSegment:
    return TranscriptSegment(
        transcript_id=transcript_id,
        order_index=order_index,
        start_char=start_char,
        end_char=end_char,
        speaker=speaker,
        content=content,
        mentioned_cases=mentioned_cases or [],
        mentioned_rules=mentioned_rules or [],
        mentioned_concepts=mentioned_concepts or [],
        sentiment_flags=sentiment_flags or [],
    )


# ---------------------------------------------------------------------------
# compute_subject_features
# ---------------------------------------------------------------------------


def test_compute_subject_features_minutes_on() -> None:
    """1 segment, 1500 chars, mentions case X → minutes_on ≈ 2.0 (1500/750).

    Q42: cpm calibrated to 750 (~150 wpm * 5 chars/word) per typical spoken
    English. Prior value of 150 was off by 5×.
    """
    seg = _segment(
        "tx",
        order_index=0,
        start_char=0,
        end_char=1500,
        content="Discussion of Shelley v. Kraemer.",
        mentioned_cases=["Shelley v. Kraemer"],
    )
    features = compute_subject_features([seg])

    assert len(features) == 1
    f = features[0]
    assert f.kind is EmphasisSubjectKind.CASE
    assert f.label == "Shelley v. Kraemer"
    assert f.minutes_on == pytest.approx(2.0, rel=1e-6)
    assert f.return_count == 1
    assert f.hypotheticals_run == []
    assert f.disclaimed is False
    assert f.engaged_questions == 0


def test_compute_subject_features_return_count_counts_distinct_segments() -> None:
    """3 segments mentioning X -> return_count == 3."""
    segs = [
        _segment(
            "tx",
            order_index=0,
            start_char=0,
            end_char=150,
            mentioned_cases=["X"],
        ),
        _segment(
            "tx",
            order_index=1,
            start_char=150,
            end_char=300,
            mentioned_cases=["X"],
        ),
        _segment(
            "tx",
            order_index=2,
            start_char=300,
            end_char=450,
            mentioned_cases=["X"],
        ),
    ]
    features = compute_subject_features(segs)
    assert len(features) == 1
    assert features[0].return_count == 3


def test_compute_subject_features_disclaimed_propagates() -> None:
    """1 segment with 'disclaimed_as_not_testable' + subject mention ->
    features.disclaimed=True."""
    seg = _segment(
        "tx",
        order_index=0,
        start_char=0,
        end_char=150,
        mentioned_cases=["Y"],
        sentiment_flags=["disclaimed_as_not_testable"],
    )
    features = compute_subject_features([seg])

    assert len(features) == 1
    assert features[0].disclaimed is True


def test_compute_subject_features_disclaimed_propagates_even_if_one_of_many() -> None:
    """If one of several segments mentioning X is disclaimed, features.disclaimed
    should still be True (the disclaimer 'wins' the aggregation)."""
    segs = [
        _segment(
            "tx",
            order_index=0,
            start_char=0,
            end_char=150,
            mentioned_cases=["X"],
        ),
        _segment(
            "tx",
            order_index=1,
            start_char=150,
            end_char=300,
            mentioned_cases=["X"],
            sentiment_flags=["disclaimed_as_not_testable"],
        ),
    ]
    features = compute_subject_features(segs)
    assert len(features) == 1
    assert features[0].disclaimed is True
    assert features[0].return_count == 2


def test_compute_subject_features_hypotheticals_captured() -> None:
    """Segment tagged `professor_hypothetical` + subject mention ->
    first 80 chars in hypotheticals_run."""
    content = (
        "Suppose the covenant bars sale to a redlined neighborhood; would the "
        "court enforce it under Shelley? Think about what happens if..."
    )
    seg = _segment(
        "tx",
        order_index=0,
        start_char=0,
        end_char=200,
        content=content,
        mentioned_cases=["Shelley v. Kraemer"],
        sentiment_flags=["professor_hypothetical"],
    )
    features = compute_subject_features([seg])
    assert len(features) == 1
    hypos = features[0].hypotheticals_run
    assert len(hypos) == 1
    assert len(hypos[0]) == 80
    assert hypos[0] == content[:80]


def test_compute_subject_features_engaged_questions_count() -> None:
    """Counts segments with the engaged-question flag that mention the subject."""
    segs = [
        _segment(
            "tx",
            order_index=0,
            start_char=0,
            end_char=150,
            mentioned_concepts=["state action doctrine"],
            sentiment_flags=["student_question_professor_engaged"],
        ),
        _segment(
            "tx",
            order_index=1,
            start_char=150,
            end_char=300,
            mentioned_concepts=["state action doctrine"],
        ),
        _segment(
            "tx",
            order_index=2,
            start_char=300,
            end_char=450,
            mentioned_concepts=["state action doctrine"],
            sentiment_flags=["student_question_professor_engaged"],
        ),
    ]
    features = compute_subject_features(segs)
    assert len(features) == 1
    assert features[0].engaged_questions == 2
    assert features[0].return_count == 3


def test_compute_subject_features_all_kinds() -> None:
    """Mixing mentioned_cases + mentioned_rules + mentioned_concepts yields
    one feature row per (kind, label)."""
    seg = _segment(
        "tx",
        order_index=0,
        start_char=0,
        end_char=150,
        mentioned_cases=["Shelley"],
        mentioned_rules=["touch and concern"],
        mentioned_concepts=["state action"],
    )
    features = compute_subject_features([seg])
    assert len(features) == 3
    kinds = {(f.kind, f.label) for f in features}
    assert (EmphasisSubjectKind.CASE, "Shelley") in kinds
    assert (EmphasisSubjectKind.RULE, "touch and concern") in kinds
    assert (EmphasisSubjectKind.CONCEPT, "state action") in kinds


# ---------------------------------------------------------------------------
# compute_provisional_score
# ---------------------------------------------------------------------------


def _real_weights() -> EmphasisWeights:
    """Load the real config weights — covered by test_emphasis_weights so
    mapper tests can assume these values."""
    from costs.emphasis_weights import load_weights as _load

    return _load()


def test_compute_provisional_score_disclaimed_depresses() -> None:
    """Identical features; only `disclaimed` differs -> disclaimed score is
    strictly lower."""
    base = EmphasisSubjectFeatures(
        kind=EmphasisSubjectKind.CASE,
        label="X",
        minutes_on=10.0,
        return_count=4,
        hypotheticals_run=["one", "two"],
        disclaimed=False,
        engaged_questions=3,
    )
    disclaimed = EmphasisSubjectFeatures(
        kind=base.kind,
        label=base.label,
        minutes_on=base.minutes_on,
        return_count=base.return_count,
        hypotheticals_run=list(base.hypotheticals_run),
        disclaimed=True,
        engaged_questions=base.engaged_questions,
    )
    w = _real_weights()
    base_score = compute_provisional_score(base, w)
    disclaimed_score = compute_provisional_score(disclaimed, w)

    assert 0.0 <= disclaimed_score < base_score <= 1.0


def test_compute_provisional_score_clamped_0_1() -> None:
    """Extreme high input saturates at 1.0; extreme low / disclaimed
    features clamp at 0.0 after the penalty."""
    extreme_high = EmphasisSubjectFeatures(
        kind=EmphasisSubjectKind.CASE,
        label="X",
        minutes_on=10_000.0,
        return_count=10_000,
        hypotheticals_run=["h"] * 1000,
        disclaimed=False,
        engaged_questions=10_000,
    )
    extreme_low_and_disclaimed = EmphasisSubjectFeatures(
        kind=EmphasisSubjectKind.CASE,
        label="X",
        minutes_on=0.0,
        return_count=0,
        hypotheticals_run=[],
        disclaimed=True,
        engaged_questions=0,
    )
    w = _real_weights()
    high = compute_provisional_score(extreme_high, w)
    low = compute_provisional_score(extreme_low_and_disclaimed, w)

    assert 0.0 <= low <= 1.0
    assert 0.0 <= high <= 1.0
    # High features but not disclaimed should pin to 1.0.
    assert high == pytest.approx(1.0)
    # Disclaimed with no positive signal should clamp to 0.0.
    assert low == pytest.approx(0.0)


def test_compute_provisional_score_midrange_is_sensible() -> None:
    """Moderate features land in a recognizably mid-range band."""
    f = EmphasisSubjectFeatures(
        kind=EmphasisSubjectKind.CASE,
        label="X",
        minutes_on=10.0,  # half the cap
        return_count=4,  # half the cap
        hypotheticals_run=["one", "two"],  # ~half the cap
        disclaimed=False,
        engaged_questions=3,  # half the cap
    )
    score = compute_provisional_score(f, _real_weights())
    # 0.5*(0.20+0.25+0.25+0.15) + 1.0*0.15 = 0.425 + 0.15 = 0.575 approx.
    assert 0.4 < score < 0.75


# ---------------------------------------------------------------------------
# build_emphasis_map — happy path
# ---------------------------------------------------------------------------


def _make_llm_payload(features: list[EmphasisSubjectFeatures]) -> str:
    """Build a valid emphasis_analysis payload from a feature list."""
    items = []
    score = 0.9
    for f in features:
        items.append(
            {
                "subject_kind": f.kind.value,
                "subject_label": f.label,
                "minutes_on": f.minutes_on,
                "return_count": f.return_count,
                "hypotheticals_run": list(f.hypotheticals_run),
                "disclaimed": f.disclaimed,
                "engaged_questions": f.engaged_questions,
                "exam_signal_score": round(score, 2),
                "justification": (
                    f"Professor returned to {f.label} {f.return_count} times."
                ),
            }
        )
        score -= 0.2
        score = max(score, 0.05)
    return json.dumps(
        {
            "items": items,
            "summary": (
                f"Top subjects: {', '.join(f.label for f in features[:3])}."
            ),
        }
    )


def test_build_emphasis_map_happy_path(temp_env: None) -> None:
    """Seed transcript + 5 segments spanning 2 cases; mock LLM; assert 2
    EmphasisItems persisted, ranked DESC."""
    engine = db.get_engine()
    with Session(engine) as session:
        transcript_id = _seed_transcript(session)

        # 5 segments: 3 mention Shelley, 2 mention River Heights.
        segs = [
            _segment(
                transcript_id,
                order_index=0,
                start_char=0,
                end_char=300,
                mentioned_cases=["Shelley v. Kraemer"],
            ),
            _segment(
                transcript_id,
                order_index=1,
                start_char=300,
                end_char=600,
                mentioned_cases=["Shelley v. Kraemer"],
                sentiment_flags=["professor_hypothetical"],
                content="Hypo on Shelley: suppose a private racially-restrictive covenant is...",
            ),
            _segment(
                transcript_id,
                order_index=2,
                start_char=600,
                end_char=900,
                mentioned_cases=["Shelley v. Kraemer"],
            ),
            _segment(
                transcript_id,
                order_index=3,
                start_char=900,
                end_char=1200,
                mentioned_cases=["River Heights"],
            ),
            _segment(
                transcript_id,
                order_index=4,
                start_char=1200,
                end_char=1500,
                mentioned_cases=["River Heights"],
                sentiment_flags=["student_question_professor_engaged"],
            ),
        ]
        for s in segs:
            session.add(s)
        session.commit()

    # Build the features the mapper will see (so we can craft a matching LLM payload).
    with Session(engine) as session:
        segments_in_db = list(
            session.exec(
                select(TranscriptSegment).where(
                    TranscriptSegment.transcript_id == transcript_id
                )
            ).all()
        )
        features = compute_subject_features(segments_in_db)

    emphasis_mapper.set_anthropic_client_factory(
        _fake_factory(lambda _n: _make_llm_payload(features))
    )

    with Session(engine) as session:
        result = build_emphasis_map(
            session,
            EmphasisMapRequest(
                corpus_id="dummy",  # feature does not re-check corpus
                transcript_id=transcript_id,
            ),
        )

    assert result.cache_hit is False
    assert len(result.items) == 2
    # Sorted DESC by exam_signal_score.
    scores = [it.exam_signal_score for it in result.items]
    assert scores == sorted(scores, reverse=True)

    # Persisted rows exist in the DB.
    with Session(engine) as session:
        rows = list(
            session.exec(
                select(EmphasisItem).where(
                    EmphasisItem.transcript_id == transcript_id
                )
            ).all()
        )
        assert len(rows) == 2

    # Mechanical fields survived to the persisted rows.
    shelley = next(
        (it for it in result.items if it.subject_label == "Shelley v. Kraemer"),
        None,
    )
    assert shelley is not None
    assert shelley.return_count == 3
    assert len(shelley.hypotheticals_run) == 1

    # Summary captured from the LLM payload.
    assert result.summary is not None and "Shelley" in result.summary


# ---------------------------------------------------------------------------
# Cache hit on second call
# ---------------------------------------------------------------------------


def test_build_emphasis_map_cache_hit_on_second_call(temp_env: None) -> None:
    engine = db.get_engine()
    with Session(engine) as session:
        transcript_id = _seed_transcript(session)
        session.add(
            _segment(
                transcript_id,
                order_index=0,
                start_char=0,
                end_char=300,
                mentioned_cases=["Shelley"],
            )
        )
        session.commit()

    factory = _fake_factory(
        lambda _n: _make_llm_payload(
            [
                EmphasisSubjectFeatures(
                    kind=EmphasisSubjectKind.CASE,
                    label="Shelley",
                    minutes_on=2.0,
                    return_count=1,
                    hypotheticals_run=[],
                    disclaimed=False,
                    engaged_questions=0,
                )
            ]
        )
    )
    emphasis_mapper.set_anthropic_client_factory(factory)

    with Session(engine) as session:
        first = build_emphasis_map(
            session,
            EmphasisMapRequest(corpus_id="c", transcript_id=transcript_id),
        )
    assert first.cache_hit is False
    calls_after_first = len(factory.holder["client"].messages.calls)

    # Second call — no force_regenerate.
    with Session(engine) as session:
        second = build_emphasis_map(
            session,
            EmphasisMapRequest(corpus_id="c", transcript_id=transcript_id),
        )
    assert second.cache_hit is True
    # Cache hit -> no additional LLM call.
    assert len(factory.holder["client"].messages.calls) == calls_after_first
    # Items came back in score DESC order (just one here).
    assert len(second.items) == 1


# ---------------------------------------------------------------------------
# force_regenerate
# ---------------------------------------------------------------------------


def test_build_emphasis_map_force_regenerate_bumps_calls(temp_env: None) -> None:
    """force_regenerate=True replaces items and calls the LLM again."""
    engine = db.get_engine()
    with Session(engine) as session:
        transcript_id = _seed_transcript(session)
        session.add(
            _segment(
                transcript_id,
                order_index=0,
                start_char=0,
                end_char=300,
                mentioned_cases=["Shelley"],
            )
        )
        session.commit()

    # Two payloads with different scores so we can verify replacement.
    def payload_fn(call_n: int) -> str:
        score = 0.9 if call_n == 1 else 0.4
        return json.dumps(
            {
                "items": [
                    {
                        "subject_kind": "case",
                        "subject_label": "Shelley",
                        "exam_signal_score": score,
                        "justification": f"call_{call_n}",
                    }
                ],
                "summary": None,
            }
        )

    factory = _fake_factory(payload_fn)
    emphasis_mapper.set_anthropic_client_factory(factory)

    with Session(engine) as session:
        first = build_emphasis_map(
            session,
            EmphasisMapRequest(corpus_id="c", transcript_id=transcript_id),
        )
    assert first.cache_hit is False
    assert pytest.approx(first.items[0].exam_signal_score, abs=1e-6) == 0.9
    assert len(factory.holder["client"].messages.calls) == 1

    with Session(engine) as session:
        second = build_emphasis_map(
            session,
            EmphasisMapRequest(
                corpus_id="c",
                transcript_id=transcript_id,
                force_regenerate=True,
            ),
        )
    assert second.cache_hit is False
    assert len(factory.holder["client"].messages.calls) == 2
    assert pytest.approx(second.items[0].exam_signal_score, abs=1e-6) == 0.4
    # No duplicate rows (unique index enforced + old rows deleted).
    with Session(engine) as session:
        rows = list(
            session.exec(
                select(EmphasisItem).where(
                    EmphasisItem.transcript_id == transcript_id
                )
            ).all()
        )
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# Missing transcript
# ---------------------------------------------------------------------------


def test_build_emphasis_map_missing_transcript_raises(temp_env: None) -> None:
    engine = db.get_engine()
    with Session(engine) as session:
        with pytest.raises(EmphasisMapError):
            build_emphasis_map(
                session,
                EmphasisMapRequest(
                    corpus_id="c",
                    transcript_id="does-not-exist-" + "0" * 40,
                ),
            )


# ---------------------------------------------------------------------------
# CostEvent emission
# ---------------------------------------------------------------------------


def test_build_emphasis_map_emits_cost_event(temp_env: None) -> None:
    engine = db.get_engine()
    with Session(engine) as session:
        transcript_id = _seed_transcript(session)
        session.add(
            _segment(
                transcript_id,
                order_index=0,
                start_char=0,
                end_char=300,
                mentioned_cases=["Shelley"],
            )
        )
        session.commit()

    emphasis_mapper.set_anthropic_client_factory(
        _fake_factory(
            lambda _n: _make_llm_payload(
                [
                    EmphasisSubjectFeatures(
                        kind=EmphasisSubjectKind.CASE,
                        label="Shelley",
                        minutes_on=2.0,
                        return_count=1,
                        hypotheticals_run=[],
                        disclaimed=False,
                        engaged_questions=0,
                    )
                ]
            )
        )
    )

    with Session(engine) as session:
        build_emphasis_map(
            session,
            EmphasisMapRequest(corpus_id="c", transcript_id=transcript_id),
        )

    with Session(engine) as session:
        rows = list(
            session.exec(
                select(CostEvent).where(CostEvent.feature == "emphasis_analysis")
            ).all()
        )
        assert len(rows) == 1
        assert rows[0].model == "claude-opus-4-7"
        assert rows[0].input_tokens == 1500
        assert rows[0].output_tokens == 900
