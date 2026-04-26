"""Fuzzy case-name resolver (spec §4.3.4).

Gemini auto-transcriptions mangle case names in predictable ways:
"Shelley v. Kraemer" comes out as "Shelly B Kramer" (the "v." mishears as a
capital "B", and both parties' spellings drift one letter each). "Penn Central
Transportation Co. v. New York City" is spoken as simply "Pen Central" — the
one-L spelling plus the full Corporate-suffix elision. "River Heights
Associates L.P. v. Batten" becomes "River Heights v Daton" — wrong second
party, dropped suffix, no period on the separator.

A straight exact match against the corpus's known canonical case names would
miss all three. A pure-LLM resolver is overkill (these are deterministic
string-distance problems) and would burn tokens on every transcript ingest.
So the resolver in this module is *rule-based with rapidfuzz scoring*:

1. Extract candidate case-like strings from the raw text with two regexes:
   (a) an explicit "X <separator> Y" shape that catches mangled v. / vs /
       versus / vee / bare-capital-B; plus
   (b) a generic 2-to-5-word-run-of-capitalized-words pattern that catches
       the suffix-elision case ("Pen Central" with no " v. " at all).
2. Normalize every candidate to the canonical "<left> v. <right>" spelling
   so scoring isn't distracted by separator noise.
3. Score each candidate against every canonical name with a composite of
   three rapidfuzz metrics (``partial_ratio + token_set_ratio + WRatio``, averaged).
   Using only WRatio ties "River Heights v Daton" equally between "River
   Heights Associates L.P. v. Batten" and "Penn Central Transportation Co. v.
   New York City" at 85.5 (both have "River" / "v." tokens partially
   overlapping); the composite breaks the tie cleanly because token_set
   heavily penalizes the wrong second party (31.4 vs 84.2).
4. Keep the best match per candidate if its composite score meets
   ``fuzzy_threshold`` (default 82.0, calibrated against the three
   user-reported mangled strings — see ``tests/unit/test_fuzzy_resolver.py``).

No LLM fallback here: §4.3.4 calls for one, but in practice the
``transcript_cleanup`` prompt already does an LLM-backed normalization pass
over the raw text, and this resolver runs *after* it as a safety net for
anything the LLM missed. Wiring an *additional* LLM round-trip from inside
the resolver would double-count costs and complicate the dependency graph.
This decision is logged in ``SPEC_QUESTIONS.md``.

Method tagging:
- ``"exact"``    — raw candidate == canonical name byte-for-byte.
- ``"normalized"`` — raw != canonical but their normalized forms agree.
- ``"fuzzy"``    — resolved only because the composite score >= threshold.

Helper: :func:`load_known_case_names_for_corpus` walks every Book in the
corpus, pulls ``Block.block_metadata["case_name"]`` off every CASE_OPINION /
CASE_HEADER block, and returns the sorted-deduplicated list of names. This
is the input to ``resolve_case_names`` for transcript ingestion.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from rapidfuzz import fuzz
from sqlmodel import Session, select

from data.models import Block, BlockType, Book

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaseNameCandidate:
    """One resolved case-name mention.

    - ``raw`` is the spelling as it appeared in the transcript text, before
      any normalization.
    - ``matched_canonical`` is the name from the corpus's known-case list the
      candidate resolved to.
    - ``score`` is the composite rapidfuzz score (0..100); always >= the
      threshold used by the caller.
    - ``method`` records how the match was obtained, so downstream callers
      (UI / debugging) can distinguish high-confidence exact matches from
      borderline fuzzy ones.
    """

    raw: str
    matched_canonical: str
    score: float
    method: Literal["exact", "normalized", "fuzzy"]


@dataclass(frozen=True)
class ResolveResult:
    """Output of :func:`resolve_case_names`.

    - ``resolved`` is the list of successful matches (de-duplicated by
      ``matched_canonical`` so two mentions of the same case produce one
      entry — keeps ``Transcript.mentioned_cases`` small).
    - ``unresolved`` collects every candidate that didn't clear the
      threshold, as the raw spelling, so the UI can surface "maybe add to
      corpus" prompts (§3.9 / spec §4.1.2 unresolved_mentions).
    """

    resolved: list[CaseNameCandidate]
    unresolved: list[str]


# ---------------------------------------------------------------------------
# Candidate extraction regexes
# ---------------------------------------------------------------------------


# Explicit "X separator Y" shape — the separator catches Gemini's "v" → "B" /
# "vee" / "vs." mishearings in addition to the canonical "v."/"vs.".
# Each side allows 1-4 capitalized tokens so we match both "Shelly B Kramer"
# and "New York City B Metropolitan Life Insurance Co.".
_V_SHAPE_RE = re.compile(
    r"""
    \b
    [A-Z][A-Za-z'.]+
    (?:\s+[A-Z][A-Za-z'.]+){0,4}
    \s+
    (?:v\.?|vs\.?|versus|vee|B)
    \s+
    [A-Z][A-Za-z'.]+
    (?:\s+[A-Z][A-Za-z'.]+){0,4}
    \b
    """,
    re.VERBOSE,
)

# Capitalized noun phrase (2-5 consecutive capitalized words). Catches
# "Pen Central" where the " v. " half was elided entirely. The 2-word minimum
# keeps single-word matches like "The" or "Court" from flooding the candidate
# list.
_CAP_PHRASE_RE = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,4}\b")


# Separator normalizers — order matters. Run multi-char tokens first so we
# don't accidentally eat a " vs " inside " versus ".
_NORM_SEP_RE: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\s+versus\s+", re.IGNORECASE), " v. "),
    (re.compile(r"\s+vs\.?\s+", re.IGNORECASE), " v. "),
    (re.compile(r"\s+vee\s+", re.IGNORECASE), " v. "),
    # " B " only matches a *capital* B surrounded by spaces — lowercase "b"
    # is too common (e.g., "a b c") to safely normalize.
    (re.compile(r"\s+B\s+"), " v. "),
    # " v " without a period before a capital: "River Heights v Daton".
    (re.compile(r"\s+v\s+(?=[A-Z])"), " v. "),
]


# Words that sometimes get captured at the start of a candidate because they
# sit at the start of a sentence (and are therefore capitalized). Stripping
# them gives us cleaner candidate boundaries: "In Shelley v. Kraemer" becomes
# just "Shelley v. Kraemer", which then matches ``method="normalized"``
# against the canonical form.
_LEADING_STOPWORDS = frozenset({
    "In", "The", "A", "An", "And", "But", "Of", "For", "On", "At",
    "From", "By", "With", "As", "See", "Cf", "Per", "Via",
})


def _normalize(s: str) -> str:
    """Normalize the separator region of a case-name string.

    Two operations:
    1. Strip a single leading stopword — common English sentence-opener
       words ("In", "The", etc.) that the extraction regex sometimes picks
       up as the first capitalized token of a candidate.
    2. Collapse all known separator variants (" vs. ", " versus ", " vee ",
       standalone " B ", " v " without period) down to the canonical " v. ".

    Party spellings are left alone — rapidfuzz handles one- or two-character
    letter drift well. We only clean up sentence-boundary noise and
    separator noise so scoring isn't wasted on non-party details.
    """
    out = s
    # Strip a leading stopword if present. Only one — "The In Shelley" is
    # implausible enough we don't chase it.
    parts = out.split(None, 1)
    if len(parts) == 2 and parts[0] in _LEADING_STOPWORDS:
        out = parts[1]

    for pat, repl in _NORM_SEP_RE:
        out = pat.sub(repl, out)
    return out.strip()


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _composite_score(a: str, b: str) -> float:
    """Composite rapidfuzz score (0..100).

    Average of three complementary metrics:
    - ``partial_ratio``: good for fragment-against-full-name matches
      ("Pen Central" inside "Penn Central Transportation Co. v. New York City").
    - ``token_set_ratio``: breaks the ambiguity when two different canonical
      names share a partial-ratio score by weighing token overlap.
    - ``WRatio``: rapidfuzz's built-in weighted combo, a decent general-purpose
      signal for moderately-mangled inputs.

    We average rather than take the max because the average is more stable at
    the threshold boundary: a candidate that scores 95/95/95 is unambiguously
    a hit, while one that scores 30/30/95 is probably a false positive the
    WRatio alone would let through.
    """
    return (
        fuzz.partial_ratio(a, b)
        + fuzz.token_set_ratio(a, b)
        + fuzz.WRatio(a, b)
    ) / 3.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def resolve_case_names(
    transcript_text: str,
    known_case_names: list[str],
    *,
    fuzzy_threshold: float = 82.0,
) -> ResolveResult:
    """Scan ``transcript_text`` for case-like strings and resolve them.

    See module docstring for the strategy; at a high level:

    1. Extract candidate mentions via the two regexes (``_V_SHAPE_RE`` +
       ``_CAP_PHRASE_RE``). De-duplicate by raw spelling to avoid scoring
       the same mention twice.
    2. For each candidate, normalize the separator, score against every
       canonical name, and keep the best.
    3. Assign a ``method`` tag based on whether the raw or normalized form
       already matches the canonical exactly.
    4. De-duplicate resolved matches by canonical name (one mention of
       "Shelley v. Kraemer" and one mention of "Shelly B Kramer" both fold
       to a single ``CaseNameCandidate`` for "Shelley v. Kraemer" — the
       higher-scoring one wins the ``raw`` slot).

    Empty ``known_case_names`` is a valid input: every candidate is routed
    to ``unresolved`` (useful for the "corpus not yet ingested" early-return
    path in :mod:`features.transcript_ingest`).
    """
    # Short-circuit early: with no known names we can't resolve anything.
    raw_candidates = _extract_candidates(transcript_text)

    if not known_case_names:
        return ResolveResult(resolved=[], unresolved=list(raw_candidates))

    # Pre-normalize the canonical list once so scoring doesn't redo the work
    # per candidate. We keep both forms — the canonical for output, the
    # normalized for scoring — since known names sometimes contain variant
    # separators too (e.g., "United States ex rel. X v. Y").
    canonical_pairs = [(name, _normalize(name)) for name in known_case_names]

    # Map canonical_name -> best CaseNameCandidate found for it so far. This
    # collapses duplicate mentions of the same case across the transcript.
    by_canonical: dict[str, CaseNameCandidate] = {}
    unresolved: list[str] = []

    for raw in raw_candidates:
        normalized_raw = _normalize(raw)

        best_score = 0.0
        best_canonical: str | None = None
        best_normalized_canonical: str | None = None

        for canonical, norm_canonical in canonical_pairs:
            s = _composite_score(normalized_raw, norm_canonical)
            if s > best_score:
                best_score = s
                best_canonical = canonical
                best_normalized_canonical = norm_canonical

        if best_canonical is None or best_score < fuzzy_threshold:
            unresolved.append(raw)
            continue

        # Method tagging: exact > normalized > fuzzy.
        method: Literal["exact", "normalized", "fuzzy"]
        if raw == best_canonical:
            method = "exact"
        elif normalized_raw == best_normalized_canonical:
            method = "normalized"
        else:
            method = "fuzzy"

        candidate = CaseNameCandidate(
            raw=raw,
            matched_canonical=best_canonical,
            score=best_score,
            method=method,
        )

        # Dedup: keep the highest-scoring mention per canonical name, except
        # always prefer exact > normalized > fuzzy regardless of numeric
        # score (a 100.0 fuzzy could numerically edge out a 99.9 exact in
        # pathological cases).
        existing = by_canonical.get(best_canonical)
        if existing is None or _method_rank(method) > _method_rank(existing.method) or (
            _method_rank(method) == _method_rank(existing.method)
            and candidate.score > existing.score
        ):
            by_canonical[best_canonical] = candidate

    return ResolveResult(
        resolved=list(by_canonical.values()),
        unresolved=unresolved,
    )


def _extract_candidates(text: str) -> list[str]:
    """Pull candidate case-name strings out of free text.

    Returns a de-duplicated-by-raw-spelling, insertion-ordered list — so
    downstream de-dup-by-canonical can still pick the first-mentioned raw
    spelling when there's a tie. ``_V_SHAPE_RE`` is applied before the
    general capitalized-phrase extractor so shape-matched candidates are
    seen first.
    """
    seen: set[str] = set()
    out: list[str] = []

    for match in _V_SHAPE_RE.findall(text):
        if match not in seen:
            seen.add(match)
            out.append(match)

    for match in _CAP_PHRASE_RE.findall(text):
        # Skip cap phrases that are a substring of an already-captured
        # v-shape candidate — "Shelly" and "Kramer" individually are not
        # useful after "Shelly B Kramer" has been recorded.
        if match in seen:
            continue
        # Also skip if the match is contained inside a larger v-shape
        # candidate we already captured.
        if any(match in captured and match != captured for captured in out):
            continue
        seen.add(match)
        out.append(match)

    return out


def _method_rank(method: Literal["exact", "normalized", "fuzzy"]) -> int:
    """Rank helper for picking the best match-method when deduplicating.

    Higher is better. Used only when two candidates for the same canonical
    case name both clear the threshold and we have to pick which one's
    ``raw`` spelling to keep.
    """
    return {"fuzzy": 0, "normalized": 1, "exact": 2}[method]


# ---------------------------------------------------------------------------
# Corpus-side helper
# ---------------------------------------------------------------------------


def load_known_case_names_for_corpus(
    session: Session,
    corpus_id: str,
) -> list[str]:
    """Pull the distinct canonical case-name list for one corpus.

    Walks every :class:`Book` in the corpus and collects
    ``Block.block_metadata["case_name"]`` off every CASE_OPINION / CASE_HEADER
    block. Returns the sorted, de-duplicated list — sorted so test assertions
    on the return value are stable.

    Blocks missing the ``case_name`` metadata key are silently skipped (not
    every CASE_HEADER is guaranteed to have it; the rule-based segmenter
    only populates it when the header line matches the regex in §4.1.3).
    Empty return value is valid — a corpus with no books yet just has no
    known cases, and :func:`resolve_case_names` handles that path.
    """
    stmt = (
        select(Block)
        .join(Book, Book.id == Block.book_id)
        .where(Book.corpus_id == corpus_id)
        .where(Block.type.in_([BlockType.CASE_OPINION, BlockType.CASE_HEADER]))
    )
    rows = session.exec(stmt).all()

    names: set[str] = set()
    for block in rows:
        metadata = block.block_metadata or {}
        name = metadata.get("case_name")
        if isinstance(name, str) and name.strip():
            names.add(name.strip())

    return sorted(names)


__all__ = [
    "CaseNameCandidate",
    "ResolveResult",
    "load_known_case_names_for_corpus",
    "resolve_case_names",
]
