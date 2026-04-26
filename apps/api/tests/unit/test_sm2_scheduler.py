"""Unit tests for the SM-2 scheduler (spec §5.3).

These tests pin the algorithm — the LLM and the spaced-repetition front-end
both depend on the exact transitions specified here, so changes to
``apply_sm2`` must come with deliberate updates to these expectations.

Reference: https://en.wikipedia.org/wiki/SuperMemo#Description_of_SM-2_algorithm
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from data.models import FlashcardReview
from features.flashcards import apply_sm2

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


def _make_state(
    *,
    ease_factor: float = 2.5,
    interval_days: int = 0,
    repetitions: int = 0,
) -> FlashcardReview:
    """Build a FlashcardReview state for testing.

    Note: not persisted — we never construct a Session in this file. The
    SM-2 transitions are pure functions over the FlashcardReview value
    object, which is the entire point.
    """
    return FlashcardReview(
        flashcard_set_id="set-1",
        card_id="card-1",
        corpus_id="corpus-1",
        ease_factor=ease_factor,
        interval_days=interval_days,
        repetitions=repetitions,
        due_at=_NOW,
    )


# ---------------------------------------------------------------------------
# Required tests (algorithm pinning)
# ---------------------------------------------------------------------------


def test_sm2_first_remember_sets_interval_1() -> None:
    """grade=4 on reps=0 → reps=1, interval=1."""
    state = _make_state(repetitions=0)
    out = apply_sm2(state, grade=4, now=_NOW)
    assert out.repetitions == 1
    assert out.interval_days == 1
    # And due_at advances by exactly that many days.
    assert out.due_at == _NOW + timedelta(days=1)


def test_sm2_second_remember_sets_interval_6() -> None:
    """grade=4 on reps=1 → reps=2, interval=6."""
    state = _make_state(repetitions=1, interval_days=1)
    out = apply_sm2(state, grade=4, now=_NOW)
    assert out.repetitions == 2
    assert out.interval_days == 6
    assert out.due_at == _NOW + timedelta(days=6)


def test_sm2_third_remember_multiplies_by_ef() -> None:
    """grade=4 on reps=2 interval=6 ef=2.5 → interval=15.

    With grade=4: delta = 0.1 - (5-4)*(0.08 + (5-4)*0.02) = 0.1 - 0.10 = 0
    so ef stays 2.5. Then interval = round(6 * 2.5) = 15.
    """
    state = _make_state(repetitions=2, interval_days=6, ease_factor=2.5)
    out = apply_sm2(state, grade=4, now=_NOW)
    assert out.repetitions == 3
    assert out.ease_factor == pytest.approx(2.5)
    assert out.interval_days == 15
    assert out.due_at == _NOW + timedelta(days=15)


def test_sm2_forget_resets_reps_and_interval() -> None:
    """grade=2 on reps=3 → reps=0, interval=1."""
    state = _make_state(repetitions=3, interval_days=15, ease_factor=2.6)
    out = apply_sm2(state, grade=2, now=_NOW)
    assert out.repetitions == 0
    assert out.interval_days == 1
    # Ease factor is unchanged on forget per the algorithm we documented.
    assert out.ease_factor == pytest.approx(2.6)
    assert out.last_grade == 2


def test_sm2_ease_factor_minimum_1_3() -> None:
    """Repeated grade=3 on a maximally-decaying ef floors at 1.3.

    grade=3: delta = 0.1 - (5-3)*(0.08 + (5-3)*0.02) = 0.1 - 2*0.12 = -0.14
    Each successful (q>=3) review subtracts 0.14 — eventually ef must hit
    the floor and stay there.
    """
    state = _make_state(repetitions=2, interval_days=10, ease_factor=2.5)
    for _ in range(20):
        state = apply_sm2(state, grade=3, now=_NOW)
    assert state.ease_factor == pytest.approx(1.3)


def test_sm2_grade_5_nudges_ef_up() -> None:
    """grade=5 → ef > previous ef (specifically by +0.1)."""
    state = _make_state(ease_factor=2.5, repetitions=2, interval_days=6)
    out = apply_sm2(state, grade=5, now=_NOW)
    # delta = 0.1 - (5-5)*(0.08 + (5-5)*0.02) = 0.1 - 0 = 0.1
    assert out.ease_factor > state.ease_factor
    assert out.ease_factor == pytest.approx(2.6)


@pytest.mark.parametrize("bad_grade", [-1, 6, 100, -100])
def test_sm2_grade_out_of_range_raises(bad_grade: int) -> None:
    """grade outside [0, 5] is a programmer error — surface loudly."""
    state = _make_state()
    with pytest.raises(ValueError):
        apply_sm2(state, grade=bad_grade, now=_NOW)


# ---------------------------------------------------------------------------
# Extra coverage — adjacent invariants worth pinning
# ---------------------------------------------------------------------------


def test_sm2_does_not_mutate_input_state() -> None:
    """``apply_sm2`` returns a NEW row — caller must be free to compare."""
    state = _make_state(repetitions=2, interval_days=6, ease_factor=2.5)
    snapshot_reps = state.repetitions
    snapshot_interval = state.interval_days
    snapshot_ef = state.ease_factor

    apply_sm2(state, grade=4, now=_NOW)

    assert state.repetitions == snapshot_reps
    assert state.interval_days == snapshot_interval
    assert state.ease_factor == snapshot_ef


def test_sm2_grade_0_full_lapse() -> None:
    """grade=0 (catastrophic forget) resets like any other forget — reps=0,
    interval=1, ef unchanged. Don't penalize ef twice for the same lapse."""
    state = _make_state(repetitions=4, interval_days=20, ease_factor=2.0)
    out = apply_sm2(state, grade=0, now=_NOW)
    assert out.repetitions == 0
    assert out.interval_days == 1
    assert out.ease_factor == pytest.approx(2.0)
    assert out.last_grade == 0


def test_sm2_records_last_reviewed_at() -> None:
    """``last_reviewed_at`` is the ``now`` we passed in."""
    state = _make_state()
    out = apply_sm2(state, grade=3, now=_NOW)
    assert out.last_reviewed_at == _NOW


def test_sm2_preserves_identity_fields() -> None:
    """SM-2 transitions touch only schedule fields; identity stays intact."""
    state = _make_state()
    out = apply_sm2(state, grade=4, now=_NOW)
    assert out.id == state.id
    assert out.flashcard_set_id == state.flashcard_set_id
    assert out.card_id == state.card_id
    assert out.corpus_id == state.corpus_id
