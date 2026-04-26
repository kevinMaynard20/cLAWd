"""Pollack-specific anti-pattern detectors (spec §5.5, Appendix A).

Rule-based pre-scan that runs BEFORE the LLM grader. Output is advisory: the
LLM still emits its own ``pattern_flags`` (which is what ships to the user) —
this module's list is captured alongside the grade for audit and to seed the
grader's attention on anti-patterns our deterministic scan already caught.

Design posture (§5.5 "calibration is anchored to the professor profile",
§2.6 "grade against the real rubric, not vibes"):

- Detectors are *defensively conservative*. Err toward false negatives, not
  false positives — a missed detection becomes an LLM-caught pattern_flag
  anyway, but a spurious rule-based flag wastes grader attention and taints
  the audit trail. Thresholds (paragraph boundaries, ±1-sentence windows,
  ±50-token windows) are tuned to keep the signal clean.
- Every detector's output uses a ``pattern_name`` slug that matches the
  ``name`` of the corresponding ``PetPeeve`` in Appendix A / the rubric's
  ``anti_patterns`` list. This is the stable key the grader joins on.
- When a professor_profile explicitly disables a pattern (via
  ``pet_peeves[*].disabled == True``), skip that detector entirely —
  the pet-peeve list is authoritative per §3.7.
- All regexes are case-insensitive and word-boundary-aware where it matters
  so ``clearly``/``Clearly``/``CLEARLY`` all land but ``Clearing the parcel``
  does not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Public result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DetectedPattern:
    """One rule-based anti-pattern hit.

    ``line_offset`` is 0-based within the full answer markdown so the UI can
    jump the editor cursor to the offending line. ``excerpt`` is verbatim
    (no normalization) so the user sees exactly what they wrote.
    """

    name: str
    severity: str  # "low" | "medium" | "high"
    excerpt: str
    line_offset: int
    message: str


# ---------------------------------------------------------------------------
# Message catalog — pattern_name → actionable hint.
# Kept in one place so every detector emits the same message for the same
# pattern (tests pin these; downstream UI depends on them).
# ---------------------------------------------------------------------------


_POLLACK_PATTERN_MESSAGES: dict[str, str] = {
    "hedge_without_resolution": (
        "Commit to a position rather than 'it depends' — identify the most "
        "likely outcome and why, then note the caveat."
    ),
    "clearly_as_argument_substitution": (
        "Drop 'clearly' and make the argument. The word 'clearly' in a brief "
        "signals there's no real argument (Pollack 2024 memo p.4)."
    ),
    "no_arguing_in_the_alternative": (
        "Argue in the alternative: when the prompt signals ambiguity, a good "
        "answer covers both plausible readings, not just the one you prefer."
    ),
    "rule_recited_not_applied": (
        "Apply the rule to THESE facts — stating a rule without tying it to "
        "the hypo's specific facts is a Pollack-named deduction."
    ),
    "conclusion_mismatches_analysis": (
        "Your 'in sum' and 'therefore' disagree — reconcile the conclusion "
        "with the preceding analysis."
    ),
    "mismatched_future_interests": (
        "Future-interest names must be legally compatible. Check the numerus "
        "clausus pairings (Appendix A item 3)."
    ),
    "read_the_prompt": (
        "Match the voice the prompt demands. Law-clerk memos are neutral; "
        "advocacy voice ('I would argue', 'my client', 'we should') loses "
        "points when the prompt asks for a detached memo."
    ),
    "ny_adverse_possession_reasonable_basis": (
        "NY adverse possession requires a REASONABLE BASIS for the belief of "
        "ownership, not merely a subjective belief."
    ),
}


# ---------------------------------------------------------------------------
# Detector plumbing
# ---------------------------------------------------------------------------


_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")
_TOKEN_RE = re.compile(r"\b[\w']+\b")

# Truncation size used to locate line offsets by substring search — short
# enough to find the sentence's anchor on the original text without carrying
# noise, long enough that two sentences rarely share a 40-char prefix.
_LINE_SEARCH_PREFIX: int = 40


def _split_paragraphs(text: str) -> list[tuple[str, int]]:
    """Return ``[(paragraph_text, starting_line_offset), ...]`` where the
    line offset is the 0-based line in ``text`` where the paragraph starts."""
    out: list[tuple[str, int]] = []
    if not text:
        return out
    # Walk line-by-line so we can track the running line offset cheaply.
    lines = text.splitlines()
    cur: list[str] = []
    cur_start = 0
    for i, line in enumerate(lines):
        if line.strip() == "":
            if cur:
                out.append(("\n".join(cur), cur_start))
                cur = []
            # Advance the tentative start past blank lines.
            cur_start = i + 1
        else:
            if not cur:
                cur_start = i
            cur.append(line)
    if cur:
        out.append(("\n".join(cur), cur_start))
    return out


def _split_sentences(paragraph: str) -> list[str]:
    """Rough sentence split — period/!/? followed by whitespace + capital.
    Good enough for heuristic pattern detection; legal prose is prickly but
    we don't need NLP-grade splitting here."""
    parts = _SENTENCE_BOUNDARY_RE.split(paragraph.strip())
    return [p.strip() for p in parts if p.strip()]


def _line_offset_of_substring(text: str, needle: str, start: int = 0) -> int:
    """Return the 0-based line number of the first occurrence of ``needle``
    in ``text`` at or after ``start``. Returns 0 if not found (caller has
    already located it, so not-found is an internal bug, not a user path)."""
    idx = text.find(needle, start)
    if idx < 0:
        return 0
    return text.count("\n", 0, idx)


def _disabled_patterns(professor_profile: dict[str, Any] | None) -> set[str]:
    """Extract any ``pet_peeves[*].disabled == True`` names so detectors can
    bail early. Robust to the common shape (``pet_peeves`` as list of dicts)
    and silently no-ops on profiles that don't carry the field."""
    if not professor_profile:
        return set()
    peeves = professor_profile.get("pet_peeves") or []
    if not isinstance(peeves, list):
        return set()
    out: set[str] = set()
    for p in peeves:
        if not isinstance(p, dict):
            continue
        if p.get("disabled") is True:
            name = p.get("name")
            if isinstance(name, str):
                out.add(name)
    return out


# ---------------------------------------------------------------------------
# 1. hedge_without_resolution
# ---------------------------------------------------------------------------


# Hedge triggers — case-insensitive. These phrases, on their own, are what
# Pollack calls "non-answers" per Appendix A.
_HEDGE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"it\s+depends\s+on\s+the\s+jurisdiction", re.IGNORECASE),
    re.compile(r"it\s+depends\s+on\s+the\s+facts", re.IGNORECASE),
    re.compile(r"it['']s\s+ultimately\s+a\s+fact\s+question", re.IGNORECASE),
    re.compile(r"the\s+court\s+would\s+need\s+to\s+evaluate\s+the\s+facts", re.IGNORECASE),
)

# Paragraph-level resolution markers — if one of these appears in the SAME
# paragraph (after the hedge), we accept the answer has moved past the hedge.
_HEDGE_RESOLUTION_RE = re.compile(
    r"\b(however|but\s+on\s+balance|on\s+balance)\b", re.IGNORECASE
)
# Commit-verb markers in a following sentence also rescue a hedge.
_COMMIT_VERB_RE = re.compile(
    r"\b(commits?|resolves?|concludes?)\b", re.IGNORECASE
)


def _detect_hedge_without_resolution(
    answer: str, paragraphs: list[tuple[str, int]]
) -> list[DetectedPattern]:
    found: list[DetectedPattern] = []
    for paragraph, para_line in paragraphs:
        # First hedge hit in this paragraph, if any.
        hedge_match: re.Match[str] | None = None
        for pat in _HEDGE_PATTERNS:
            m = pat.search(paragraph)
            if m is not None and (hedge_match is None or m.start() < hedge_match.start()):
                hedge_match = m
        if hedge_match is None:
            continue
        # Resolved later in the same paragraph?
        tail = paragraph[hedge_match.end() :]
        if _HEDGE_RESOLUTION_RE.search(tail):
            continue
        # Commit verb in the next 1-2 sentences of this paragraph?
        sentences = _split_sentences(paragraph)
        # Find which sentence contains the hedge.
        running = 0
        hedge_sentence_idx = 0
        for i, s in enumerate(sentences):
            running_end = running + len(s)
            if running <= hedge_match.start() <= running_end + 4:
                hedge_sentence_idx = i
                break
            running = running_end + 1
        follow = " ".join(sentences[hedge_sentence_idx + 1 : hedge_sentence_idx + 3])
        if _COMMIT_VERB_RE.search(follow):
            continue
        excerpt = hedge_match.group(0)
        # Line offset: count newlines from paragraph start to hedge position.
        line_within = paragraph.count("\n", 0, hedge_match.start())
        found.append(
            DetectedPattern(
                name="hedge_without_resolution",
                severity="high",
                excerpt=excerpt,
                line_offset=para_line + line_within,
                message=_POLLACK_PATTERN_MESSAGES["hedge_without_resolution"],
            )
        )
    return found


# ---------------------------------------------------------------------------
# 2. clearly_as_argument_substitution
# ---------------------------------------------------------------------------


# \bclearly\b — every hit is its own flag.
_CLEARLY_RE = re.compile(r"\bclearly\b", re.IGNORECASE)


def _detect_clearly_substitution(answer: str) -> list[DetectedPattern]:
    found: list[DetectedPattern] = []
    for m in _CLEARLY_RE.finditer(answer):
        # Grab a small verbatim window for the excerpt so the UI can show
        # context without truncating the offending token.
        start = max(0, m.start() - 30)
        end = min(len(answer), m.end() + 30)
        excerpt = answer[start:end].strip()
        line = answer.count("\n", 0, m.start())
        found.append(
            DetectedPattern(
                name="clearly_as_argument_substitution",
                severity="high",
                excerpt=excerpt,
                line_offset=line,
                message=_POLLACK_PATTERN_MESSAGES["clearly_as_argument_substitution"],
            )
        )
    return found


# ---------------------------------------------------------------------------
# 3. no_arguing_in_the_alternative
# ---------------------------------------------------------------------------


_ARGUE_TRIGGER_RE = re.compile(
    r"(could\s+be\s+argued\s+that|one\s+might\s+argue)", re.IGNORECASE
)
_ALTERNATIVE_MARKERS: tuple[re.Pattern[str], ...] = (
    re.compile(r"in\s+the\s+alternative", re.IGNORECASE),
    re.compile(r"\bhowever\b", re.IGNORECASE),
    re.compile(r"but\s+the\s+court\s+could\s+also", re.IGNORECASE),
    re.compile(r"a\s+counter\s*argument\s+is", re.IGNORECASE),
)


def _detect_no_alternative_argument(answer: str) -> list[DetectedPattern]:
    hits = list(_ARGUE_TRIGGER_RE.finditer(answer))
    # Heuristic: the pattern fires only when the student committed to ONE
    # argument-framing sentence and never balanced it. Two+ hits usually
    # indicate the answer is already argumentative / exploratory.
    if len(hits) != 1:
        return []
    for marker in _ALTERNATIVE_MARKERS:
        if marker.search(answer):
            return []
    only = hits[0]
    start = max(0, only.start() - 30)
    end = min(len(answer), only.end() + 30)
    excerpt = answer[start:end].strip()
    return [
        DetectedPattern(
            name="no_arguing_in_the_alternative",
            severity="high",
            excerpt=excerpt,
            line_offset=answer.count("\n", 0, only.start()),
            message=_POLLACK_PATTERN_MESSAGES["no_arguing_in_the_alternative"],
        )
    ]


# ---------------------------------------------------------------------------
# 4. rule_recited_not_applied
# ---------------------------------------------------------------------------


# Sentence-level rule phrases — deliberately narrow so non-rule prose
# ("the rule is simple") doesn't spuriously fire. "the test" is arguably
# broad but Pollack's memos use it as a rule-framing signal ("the test is
# balancing"); we accept the risk for fidelity to Appendix A.
_RULE_PHRASE_RE = re.compile(
    r"\b(rule\s+is|doctrine\s+holds|the\s+test|the\s+standard)\b",
    re.IGNORECASE,
)
# Application markers — any of these within the next 2 sentences rescues
# the rule sentence.
_APPLICATION_MARKERS_RE = re.compile(
    r"\b(here|this\s+case|the\s+facts)\b", re.IGNORECASE
)


def _detect_rule_recited_not_applied(answer: str) -> list[DetectedPattern]:
    found: list[DetectedPattern] = []
    sentences = _split_sentences(answer)
    for i, s in enumerate(sentences):
        if not _RULE_PHRASE_RE.search(s):
            continue
        window = " ".join(sentences[i + 1 : i + 3])
        if _APPLICATION_MARKERS_RE.search(window):
            continue
        # Skip when the rule sentence itself already includes application
        # language — the student may have front-loaded the apply step.
        if _APPLICATION_MARKERS_RE.search(s):
            continue
        line = _line_offset_of_substring(answer, s[:_LINE_SEARCH_PREFIX] if len(s) > _LINE_SEARCH_PREFIX else s)
        found.append(
            DetectedPattern(
                name="rule_recited_not_applied",
                severity="high",
                excerpt=s,
                line_offset=line,
                message=_POLLACK_PATTERN_MESSAGES["rule_recited_not_applied"],
            )
        )
    return found


# ---------------------------------------------------------------------------
# 5. conclusion_mismatches_analysis
# ---------------------------------------------------------------------------


_IN_SUM_RE = re.compile(
    r"in\s+sum,\s+(?P<body>.*?)(?:\.|$)", re.IGNORECASE | re.DOTALL
)
_THEREFORE_RE = re.compile(
    r"therefore,?\s+(?P<body>.*?)(?:\.|$)", re.IGNORECASE | re.DOTALL
)
_PARTY_TOKEN_RE = re.compile(r"\b[A-Z][a-z]+\b")


def _detect_conclusion_mismatch(answer: str) -> list[DetectedPattern]:
    sum_m = _IN_SUM_RE.search(answer)
    if sum_m is None:
        return []
    thr_m = _THEREFORE_RE.search(answer, pos=sum_m.end())
    if thr_m is None:
        # Look before the "in sum" — some students conclude, then summarize.
        thr_m = _THEREFORE_RE.search(answer, 0, sum_m.start())
    if thr_m is None:
        return []
    sum_body = sum_m.group("body").strip()
    thr_body = thr_m.group("body").strip()
    if not sum_body or not thr_body:
        return []
    # Contradiction signals (conservative):
    #  (a) one contains " not " and the other doesn't; OR
    #  (b) Capitalized party-tokens differ (different party names).
    sum_has_not = bool(re.search(r"\bnot\b", sum_body, re.IGNORECASE))
    thr_has_not = bool(re.search(r"\bnot\b", thr_body, re.IGNORECASE))
    contradiction = sum_has_not != thr_has_not
    if not contradiction:
        sum_parties = set(_PARTY_TOKEN_RE.findall(sum_body)) - {"The", "This", "That"}
        thr_parties = set(_PARTY_TOKEN_RE.findall(thr_body)) - {"The", "This", "That"}
        # Only flag on party mismatch when BOTH sentences actually named
        # parties — otherwise we'd false-positive on neutral conclusions.
        if sum_parties and thr_parties and sum_parties.isdisjoint(thr_parties):
            contradiction = True
    if not contradiction:
        return []
    excerpt = f"In sum, {sum_body}. Therefore, {thr_body}."
    # Report at the later of the two matches so the UI highlights the
    # conclusion that failed to reconcile.
    anchor = max(sum_m.start(), thr_m.start())
    return [
        DetectedPattern(
            name="conclusion_mismatches_analysis",
            severity="medium",
            excerpt=excerpt[:240],
            line_offset=answer.count("\n", 0, anchor),
            message=_POLLACK_PATTERN_MESSAGES["conclusion_mismatches_analysis"],
        )
    ]


# ---------------------------------------------------------------------------
# 6. mismatched_future_interests (Appendix A item 3)
# ---------------------------------------------------------------------------


_FI_CONTINGENT_RE = re.compile(r"contingent\s+remainder", re.IGNORECASE)
_FI_VESTED_OPEN_RE = re.compile(
    r"vested\s+remainder\s+subject\s+to\s+open", re.IGNORECASE
)
_FI_INDEFEASIBLY_VESTED_RE = re.compile(r"indefeasibly\s+vested", re.IGNORECASE)
_FI_VESTED_DIVESTMENT_RE = re.compile(
    r"vested\s+remainder\s+subject\s+to\s+complete\s+divestment",
    re.IGNORECASE,
)
_FI_EXECUTORY_RE = re.compile(r"executory\s+interest", re.IGNORECASE)
# Any other future-interest token we might collide with indefeasibly_vested.
_FI_OTHER_INTEREST_RES: tuple[re.Pattern[str], ...] = (
    _FI_CONTINGENT_RE,
    _FI_VESTED_OPEN_RE,
    _FI_VESTED_DIVESTMENT_RE,
    _FI_EXECUTORY_RE,
)


def _detect_mismatched_future_interests(answer: str) -> list[DetectedPattern]:
    found: list[DetectedPattern] = []
    sentences = _split_sentences(answer)
    for s in sentences:
        # Pair 1: contingent + vested-subject-to-open in one sentence.
        if _FI_CONTINGENT_RE.search(s) and _FI_VESTED_OPEN_RE.search(s):
            line = _line_offset_of_substring(answer, s[:_LINE_SEARCH_PREFIX] if len(s) > _LINE_SEARCH_PREFIX else s)
            found.append(
                DetectedPattern(
                    name="mismatched_future_interests",
                    severity="high",
                    excerpt=s,
                    line_offset=line,
                    message=_POLLACK_PATTERN_MESSAGES["mismatched_future_interests"],
                )
            )
            continue  # avoid double-firing on the same sentence

        # Pair 2: indefeasibly vested + any other future interest.
        if _FI_INDEFEASIBLY_VESTED_RE.search(s):
            other = any(pat.search(s) for pat in _FI_OTHER_INTEREST_RES)
            if other:
                line = _line_offset_of_substring(answer, s[:_LINE_SEARCH_PREFIX] if len(s) > _LINE_SEARCH_PREFIX else s)
                found.append(
                    DetectedPattern(
                        name="mismatched_future_interests",
                        severity="high",
                        excerpt=s,
                        line_offset=line,
                        message=_POLLACK_PATTERN_MESSAGES["mismatched_future_interests"],
                    )
                )
                continue

    # Pair 3: vested_subject_to_complete_divestment without executory_interest
    # within ±1 sentence. Walk with an explicit window so we stay conservative.
    for i, s in enumerate(sentences):
        if _FI_VESTED_DIVESTMENT_RE.search(s):
            window_start = max(0, i - 1)
            window_end = min(len(sentences), i + 2)
            window = " ".join(sentences[window_start:window_end])
            if not _FI_EXECUTORY_RE.search(window):
                line = _line_offset_of_substring(answer, s[:_LINE_SEARCH_PREFIX] if len(s) > _LINE_SEARCH_PREFIX else s)
                found.append(
                    DetectedPattern(
                        name="mismatched_future_interests",
                        severity="high",
                        excerpt=s,
                        line_offset=line,
                        message=_POLLACK_PATTERN_MESSAGES["mismatched_future_interests"],
                    )
                )
    return found


# ---------------------------------------------------------------------------
# 7. read_the_prompt (voice markers)
# ---------------------------------------------------------------------------


_VOICE_ADVOCATE_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bI\s+would\s+argue\b"),
    re.compile(r"\bmy\s+client\b", re.IGNORECASE),
    re.compile(r"\bwe\s+should\b", re.IGNORECASE),
)


def _detect_voice_violation(answer: str) -> list[DetectedPattern]:
    for pat in _VOICE_ADVOCATE_RES:
        m = pat.search(answer)
        if m is not None:
            start = max(0, m.start() - 20)
            end = min(len(answer), m.end() + 40)
            return [
                DetectedPattern(
                    name="read_the_prompt",
                    severity="high",
                    excerpt=answer[start:end].strip(),
                    line_offset=answer.count("\n", 0, m.start()),
                    message=_POLLACK_PATTERN_MESSAGES["read_the_prompt"],
                )
            ]
    return []


# ---------------------------------------------------------------------------
# 8. ny_adverse_possession_reasonable_basis
# ---------------------------------------------------------------------------


# Pollack's memo-flagged phrase is "thought/believed they owned" but students
# interchangeably use "she/he/they/the plaintiff/etc." — accept any subject
# pronoun or short noun between the verb and "owned" so we catch the pattern
# as written in real answers.
_AP_BELIEF_RE = re.compile(
    r"\b(thought|believed)\s+(?:they|he|she|it|the\s+\w+)\s+owned\b",
    re.IGNORECASE,
)
_AP_REASONABLE_RE = re.compile(r"\breasonable\s+basis\b", re.IGNORECASE)


def _detect_ny_adverse_possession(answer: str) -> list[DetectedPattern]:
    found: list[DetectedPattern] = []
    tokens = list(_TOKEN_RE.finditer(answer))
    # Build a mapping from char offset → token index for the ±50-token window.
    char_to_tok: list[int] = [0] * (len(answer) + 1)
    for i, t in enumerate(tokens):
        # Everything from prior end up to this token's end maps to i.
        for pos in range(t.start(), min(t.end() + 1, len(char_to_tok))):
            char_to_tok[pos] = i
    for m in _AP_BELIEF_RE.finditer(answer):
        # Tokens in [tok_idx - 50, tok_idx + 50] must contain "reasonable basis".
        center = char_to_tok[m.start()] if m.start() < len(char_to_tok) else 0
        lo = max(0, center - 50)
        hi = min(len(tokens), center + 50)
        if lo >= hi:
            window_text = ""
        else:
            window_text = answer[tokens[lo].start() : tokens[hi - 1].end()]
        if _AP_REASONABLE_RE.search(window_text):
            continue
        start = max(0, m.start() - 20)
        end = min(len(answer), m.end() + 40)
        found.append(
            DetectedPattern(
                name="ny_adverse_possession_reasonable_basis",
                severity="medium",
                excerpt=answer[start:end].strip(),
                line_offset=answer.count("\n", 0, m.start()),
                message=_POLLACK_PATTERN_MESSAGES["ny_adverse_possession_reasonable_basis"],
            )
        )
    return found


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def scan_answer(
    answer_markdown: str,
    *,
    professor_profile: dict[str, Any] | None = None,
) -> list[DetectedPattern]:
    """Run every Pollack pattern detector.

    When ``professor_profile`` carries a ``pet_peeves`` list with an item
    flagged ``disabled: True``, the matching detector is skipped — the
    profile is authoritative (§3.7).

    Returns a single consolidated list; order is: hedging → 'clearly' → no
    alternative → rule-not-applied → conclusion mismatch → future-interest
    mismatch → voice violation → adverse-possession. Tests pin detector
    content, not order — but keeping the emission order stable makes diffs
    readable when the list grows across runs.
    """
    if not answer_markdown:
        return []

    disabled = _disabled_patterns(professor_profile)
    paragraphs = _split_paragraphs(answer_markdown)
    out: list[DetectedPattern] = []

    def _run(name: str, fn):
        if name in disabled:
            return
        out.extend(fn())

    _run(
        "hedge_without_resolution",
        lambda: _detect_hedge_without_resolution(answer_markdown, paragraphs),
    )
    _run(
        "clearly_as_argument_substitution",
        lambda: _detect_clearly_substitution(answer_markdown),
    )
    _run(
        "no_arguing_in_the_alternative",
        lambda: _detect_no_alternative_argument(answer_markdown),
    )
    _run(
        "rule_recited_not_applied",
        lambda: _detect_rule_recited_not_applied(answer_markdown),
    )
    _run(
        "conclusion_mismatches_analysis",
        lambda: _detect_conclusion_mismatch(answer_markdown),
    )
    _run(
        "mismatched_future_interests",
        lambda: _detect_mismatched_future_interests(answer_markdown),
    )
    _run(
        "read_the_prompt",
        lambda: _detect_voice_violation(answer_markdown),
    )
    _run(
        "ny_adverse_possession_reasonable_basis",
        lambda: _detect_ny_adverse_possession(answer_markdown),
    )

    return out


__all__ = [
    "DetectedPattern",
    "scan_answer",
]
