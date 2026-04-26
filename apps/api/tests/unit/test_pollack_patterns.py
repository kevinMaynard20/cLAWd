"""Unit tests for features/pollack_patterns.py (spec §5.5 + Appendix A).

Each of the eight detectors gets at least one "should fire" case and a
companion "should NOT fire" case that pins the conservative threshold
the spec asks for. A clean IRAC answer produces zero detections.
"""

from __future__ import annotations

from features.pollack_patterns import DetectedPattern, scan_answer


def _names(patterns: list[DetectedPattern]) -> list[str]:
    return [p.name for p in patterns]


# ---------------------------------------------------------------------------
# 1. hedge_without_resolution
# ---------------------------------------------------------------------------


def test_hedges_without_resolution_flagged() -> None:
    out = scan_answer("It depends on the jurisdiction.")
    assert "hedge_without_resolution" in _names(out)
    assert out[0].severity == "high"

    # Paragraph-level resolution via "however, on balance" rescues the hedge.
    clean = (
        "It depends on the jurisdiction; however, on balance the court would "
        "find the covenant enforceable under New York law."
    )
    assert "hedge_without_resolution" not in _names(scan_answer(clean))


# ---------------------------------------------------------------------------
# 2. clearly_as_argument_substitution
# ---------------------------------------------------------------------------


def test_clearly_as_argument_substitution_flagged() -> None:
    out = scan_answer("Clearly, the rule applies.")
    assert "clearly_as_argument_substitution" in _names(out)
    assert out[0].severity == "high"

    # Word-boundary — "Clearing the parcel" is not a hit.
    assert not scan_answer("Clearing the parcel was permitted by the grantor.")


# ---------------------------------------------------------------------------
# 3. no_arguing_in_the_alternative
# ---------------------------------------------------------------------------


def test_no_arguing_in_the_alternative_flagged() -> None:
    # Single "could be argued that" with no alternative markers → flag.
    answer = (
        "It could be argued that the covenant runs with the land. The grantor "
        "intended horizontal privity."
    )
    out = scan_answer(answer)
    assert "no_arguing_in_the_alternative" in _names(out)


def test_arguing_in_the_alternative_not_flagged() -> None:
    answer = (
        "It could be argued that the covenant runs with the land. In the "
        "alternative, a court might find no horizontal privity and refuse to "
        "enforce it."
    )
    assert "no_arguing_in_the_alternative" not in _names(scan_answer(answer))


# ---------------------------------------------------------------------------
# 4. rule_recited_not_applied
# ---------------------------------------------------------------------------


def test_rule_recited_not_applied_flagged() -> None:
    answer = (
        "The rule is that adverse possession requires continuous use for the "
        "statutory period. The grantor conveyed the land in 1980. The original "
        "deed was duly recorded."
    )
    out = scan_answer(answer)
    assert "rule_recited_not_applied" in _names(out)


def test_rule_applied_not_flagged() -> None:
    answer = (
        "The rule is that adverse possession requires continuous use. Here, "
        "Sarah has used the strip for 15 years without interruption."
    )
    assert "rule_recited_not_applied" not in _names(scan_answer(answer))


# ---------------------------------------------------------------------------
# 5. conclusion_mismatches_analysis
# ---------------------------------------------------------------------------


def test_conclusion_mismatches_analysis_flagged() -> None:
    # "in sum, the covenant is enforceable. therefore, it is not enforceable."
    answer = (
        "The grantor demonstrated intent and privity. "
        "In sum, the covenant is enforceable. "
        "Therefore, it is not enforceable."
    )
    out = scan_answer(answer)
    assert "conclusion_mismatches_analysis" in _names(out)
    flag = next(p for p in out if p.name == "conclusion_mismatches_analysis")
    assert flag.severity == "medium"


# ---------------------------------------------------------------------------
# 6. mismatched_future_interests
# ---------------------------------------------------------------------------


def test_mismatched_future_interests_flagged() -> None:
    # Pair 1: contingent remainder AND vested remainder subject to open.
    answer = (
        "The grantee takes a contingent remainder and a vested remainder "
        "subject to open."
    )
    assert "mismatched_future_interests" in _names(scan_answer(answer))


def test_mismatched_future_interests_indefeasibly_vested_flagged() -> None:
    answer = (
        "The grantee holds an indefeasibly vested interest and also a "
        "contingent remainder."
    )
    assert "mismatched_future_interests" in _names(scan_answer(answer))


def test_mismatched_future_interests_divestment_without_executory_flagged() -> None:
    answer = "The grantee holds a vested remainder subject to complete divestment."
    assert "mismatched_future_interests" in _names(scan_answer(answer))


def test_mismatched_future_interests_divestment_with_executory_not_flagged() -> None:
    answer = (
        "The grantee holds a vested remainder subject to complete divestment. "
        "The executory interest follows as a shifting interest."
    )
    assert "mismatched_future_interests" not in _names(scan_answer(answer))


# ---------------------------------------------------------------------------
# 7. read_the_prompt (voice violation)
# ---------------------------------------------------------------------------


def test_voice_violation_flagged() -> None:
    answer = "I would argue that my client's position is strong."
    assert "read_the_prompt" in _names(scan_answer(answer))


def test_neutral_memo_voice_not_flagged() -> None:
    answer = (
        "The court should consider whether the covenant runs with the land. "
        "A reviewing court would likely find horizontal privity satisfied."
    )
    assert "read_the_prompt" not in _names(scan_answer(answer))


# ---------------------------------------------------------------------------
# 8. ny_adverse_possession_reasonable_basis
# ---------------------------------------------------------------------------


def test_ny_adverse_possession_reasonable_basis_flagged() -> None:
    answer = "She thought she owned the strip of land for twenty years."
    assert "ny_adverse_possession_reasonable_basis" in _names(scan_answer(answer))


def test_ny_adverse_possession_with_reasonable_basis_not_flagged() -> None:
    answer = (
        "She thought she owned the strip and had a reasonable basis to do so, "
        "given the deed's metes-and-bounds description."
    )
    assert "ny_adverse_possession_reasonable_basis" not in _names(scan_answer(answer))


# ---------------------------------------------------------------------------
# Clean answer
# ---------------------------------------------------------------------------


def test_clean_answer_no_detections() -> None:
    answer = (
        "The first issue is whether the covenant runs with the land. "
        "The doctrine requires intent, privity, and touch-and-concern. "
        "Here, the grantor's deed expresses intent and Sarah stands in "
        "vertical privity with the original promisor; the restriction "
        "touches and concerns the servient estate because it affects its "
        "use. Accordingly, the covenant runs with the land and binds Sarah."
    )
    assert scan_answer(answer) == []


# ---------------------------------------------------------------------------
# Disabled patterns via professor profile
# ---------------------------------------------------------------------------


def test_scan_answer_respects_disabled_patterns() -> None:
    answer = "Clearly, the rule applies."
    profile = {
        "pet_peeves": [
            {"name": "clearly_as_argument_substitution", "disabled": True},
        ]
    }
    out = scan_answer(answer, professor_profile=profile)
    assert "clearly_as_argument_substitution" not in _names(out)


def test_enabled_pattern_still_fires_when_other_disabled() -> None:
    # Disabling "clearly" does not suppress unrelated patterns.
    answer = "Clearly, the rule applies. It depends on the jurisdiction."
    profile = {
        "pet_peeves": [
            {"name": "clearly_as_argument_substitution", "disabled": True},
        ]
    }
    out = scan_answer(answer, professor_profile=profile)
    assert "hedge_without_resolution" in _names(out)
    assert "clearly_as_argument_substitution" not in _names(out)
