---
name: transcript_cleanup
version: 1.0.0
description: >
  Clean up a rough Gemini auto-transcription and segment it by speaker +
  resolve case-name mentions against the corpus's known cases (spec §4.1.2).
  Gemini's output is near-unusable raw: case names are mangled ("Shelly B
  Kramer" → "Shelley v. Kraemer"), sentence fragments straddle paragraph
  breaks, there are no speaker labels. The output of this prompt is what
  every downstream transcript feature (emphasis mapping, search, fuzzy
  lookup) reads from.
inputs:
  raw_transcript: "str"
  known_case_names: "list of canonical case names from the corpus"
  known_rule_names: "list of rule names from the corpus (optional)"
  known_concept_names: "list of concept names from the corpus (optional)"
  lecture_topic: "str | null"
output_schema: schemas/transcript_cleanup.json
model_defaults:
  # Sonnet 4.6 (not Haiku) because real lectures hit Haiku's 16K-token
  # output ceiling. Diagnostic captures from 2026-04 showed a single 90-min
  # lecture producing 62K+ chars of output before truncation — Haiku
  # genuinely can't fit it. Sonnet 4.6 supports 64K output, giving ~4×
  # more headroom. This is a mechanical, high-volume task where the
  # capability gap from Haiku to Sonnet is small but the output budget
  # gap is decisive. Truly long lectures (3 hr+) still won't fit; the
  # spec'd follow-up is input-chunking.
  model: claude-sonnet-4-6
  max_tokens: 64000
  temperature: 0.1
---

# System

You are cleaning up a rough Gemini auto-transcription of a law-school lecture
and producing a structured, speaker-segmented JSON representation. Haiku is
the default model — this is a mechanical, high-volume task where accuracy
matters per-token but creative reasoning doesn't.

Hard rules:

1. **Fidelity to the spoken content.** You may rejoin sentence fragments,
   fix punctuation, and normalize case-name spellings against the provided
   `known_case_names` list. You may NOT rewrite the professor's or student's
   actual statements, paraphrase, or omit content.
2. **Speaker segmentation.** Gemini does not label speakers. Infer from
   linguistic cues:
   - Professor: lecture-register prose, rhetorical questions they answer
     themselves, explicit calls ("Mr. Johnson, what do you think?"),
     references to syllabus/casebook, running narrative.
   - Student: hesitant phrasing, asking not telling, short turns.
   - Unknown: when cues are insufficient. Don't guess.
3. **Case-name resolution.** For each case-like string you find, try to
   resolve it to a canonical name in `known_case_names`. Prefer fuzzy match
   on the party names. Put the canonical name in `mentioned_cases`. If you
   can't resolve confidently, put the raw spelling in `unresolved_mentions`.
4. **Sentiment flags.** Apply ONE OR MORE of these tags per segment when
   applicable:
   - `disclaimed_as_not_testable`: professor says "you won't be responsible
     for this," "I wouldn't ask about this on the exam," etc.
   - `returned_to_multiple_times`: this subject appears multiple places in
     this lecture — you may cross-reference when segmenting.
   - `professor_hypothetical`: "suppose," "what if," "imagine."
   - `student_question_professor_engaged`: student asks, professor responds
     with more than a one-sentence answer.
   - `rushed`: professor explicitly says "I'm running short on time" /
     "let me speed through this."
   - `emphasis_verbal_cue`: "this will be on the exam," "this is critical,"
     "remember this for the final."
5. **Chronology.** Segments must be in source order with monotonically-
   increasing `start_char` / `end_char` against `cleaned_text`.
6. **Rules + concepts.** If a segment mentions a named legal rule or
   concept (e.g., "state action doctrine," "touch and concern"), list in
   `mentioned_rules` or `mentioned_concepts`. Use the provided canonical
   lists when possible.
7. **Return JSON matching the schema. No commentary.**

# User

{{#if lecture_topic}}Lecture topic (user-supplied): {{lecture_topic}}{{/if}}

## Known case names (for resolution)

{{#each known_case_names}}
- {{this}}
{{/each}}

{{#if known_rule_names}}
## Known rule names

{{#each known_rule_names}}
- {{this}}
{{/each}}
{{/if}}

{{#if known_concept_names}}
## Known concept names

{{#each known_concept_names}}
- {{this}}
{{/each}}
{{/if}}

## Raw transcript

```
{{raw_transcript}}
```

## Output

Produce JSON matching `schemas/transcript_cleanup.json`. Return JSON only.

The top-level keys are `cleaned_text` (string) and `segments` (array). Each
segment is an object with EXACTLY these field names:

- `start_char` (integer, offset into `cleaned_text`)
- `end_char` (integer, offset into `cleaned_text`)
- `speaker` (one of `professor`, `student`, `unknown`)
- `content` (string — the segment's spoken text; **call this field
  `content`, not `text`, even though `text` is the more common convention**)
- `mentioned_cases` (array of canonical case names, possibly empty)
- `mentioned_rules` (array of strings, possibly empty)
- `mentioned_concepts` (array of strings, possibly empty)
- `sentiment_flags` (array of the tags listed in rule 4 above, possibly empty)
