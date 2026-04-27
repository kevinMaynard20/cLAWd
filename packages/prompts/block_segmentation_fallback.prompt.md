---
name: block_segmentation_fallback
version: 1.0.0
description: >
  Classify an ambiguous markdown chunk into one of the canonical block types
  (spec §3.5). Called by the ingest pipeline when the rule-based segmenter
  (§4.1.3) can't confidently type a chunk. Low-stakes per-call (Haiku is the
  default model, §7.7.6), but correctness at scale matters: a mis-typed
  numbered_note bleeds into case-brief generation downstream.
inputs:
  markdown_chunk: str
  preceding_block_type: str | null
  following_block_type: str | null
  source_page: int
output_schema: schemas/block_classification.json
model_defaults:
  model: claude-haiku-4-5
  max_tokens: 16000
  temperature: 0
---

# System

You are classifying a chunk of casebook markdown into one of these block types:

- `narrative_text` — running prose that isn't part of a court opinion, header, note, or footnote.
- `case_opinion` — the body of a published judicial opinion. Typically includes "held," "affirm," "remand," "concur," party-name references, court-formal prose.
- `case_header` — the title of a case, with or without court/year/citation. Usually short (one line, or a line plus a court/year).
- `numbered_note` — a casebook's post-case note that starts with a number + period (`1. ...`, `2. ...`). May include multiple paragraphs.
- `problem` — a standalone hypothetical the casebook author asks the student to work through. Usually introduced with a literal "Problem" label.
- `footnote` — a short paragraph anchored by a numeric superscript, usually at the page bottom.
- `block_quote` — text quoted from a statute, restatement, or secondary source. In markdown this often appears with `>` line prefixes.
- `header` — a markdown section header (`# Part I`, `## Chapter 10`), NOT a case header.
- `figure` — an image or diagram.
- `table` — tabular content.

Pick the single type that best fits. If the chunk is genuinely unclear or mixes types, pick `narrative_text` as the safe fallback.

Also extract per-type metadata per spec §3.5:

- `case_opinion`: `{court?, year?, citation?, judge?, case_name?}` — fill from the text when visible; leave a field out when absent.
- `case_header`: `{case_name}` — the party-v-party string.
- `numbered_note`: `{number: int, has_problem: bool}` — set `has_problem=true` when the note's body contains "Problem:" or a labelled Problem block.
- `footnote`: `{footnote_number: int}` — parsed from the leading digit.
- `header`: `{level: int, text: str}` — level from the leading `#` count; `text` with `#`s stripped.
- Others: empty dict.

Never guess metadata that isn't supported by the text.

# User

Source page: {{ source_page }}

Preceding block type: {{ preceding_block_type }}
Following block type: {{ following_block_type }}

Chunk to classify:

```
{{ markdown_chunk }}
```

Return JSON matching the schema. No commentary.
