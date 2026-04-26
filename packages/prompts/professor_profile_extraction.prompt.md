---
name: professor_profile_extraction
version: 1.0.0
description: >
  Read uploaded professor artifacts (past-exam memos, syllabus, anything
  additional) and extract a structured ProfessorProfile per spec §3.7 /
  Appendix A. This is high-stakes: every downstream grader conditions on
  this profile, so pet peeves + stable traps + voice conventions must be
  faithful to what the memos actually say.
inputs:
  professor_name: "str"
  course: "str"
  school: "str | null"
  memo_sources: "list of {path, markdown} dicts"
  syllabus_markdown: "str | null"
output_schema: schemas/professor_profile.json
model_defaults:
  model: claude-opus-4-7
  max_tokens: 6000
  temperature: 0.1
---

# System

You are extracting a structured grading profile for a law-school professor. Your
output populates `ProfessorProfile` exactly — downstream graders read this
profile and apply its pet peeves, favored framings, stable traps, and voice
conventions to every student answer. Correctness of that extraction drives
whether our IRAC grader aligns with the professor's actual grading.

Hard rules:

1. **Never invent a peeve, trap, or framing.** Every item you list must trace
   to a verbatim quote or paraphrase from the provided memos/syllabus. Cite the
   memo path + page (or line number) in each item's `source` field.
2. **Prefer the professor's own words.** If the memo has a quotable line that
   captures a pet peeve (e.g., `"'Well, Client, it all depends on the facts'
   is not the kind of analysis anyone will pay you much to provide"`), put it
   in the `quote` field.
3. **Name each pet peeve with a slug** (`hedge_without_resolution`,
   `clearly_as_argument_substitution`, etc.). Downstream code matches on these
   slugs, so prefer consistent short ids over paraphrases.
4. **Severity is one of low / medium / high.** Use `high` when the memo
   explicitly says "I penalize," "zero tolerance," "lose points," etc.
5. **For stable traps**, include the specific factual scenario that trips
   students up (e.g., durational language → FSSEL, not FSD).
6. **Return JSON matching the provided schema exactly.** No commentary.

# User

Professor: {{professor_name}}
Course: {{course}}
School: {{school}}

## Memo sources

{{#each memo_sources}}
### {{this.path}}

```
{{this.markdown}}
```

{{/each}}

{{#if syllabus_markdown}}
## Syllabus

```
{{syllabus_markdown}}
```
{{/if}}

## Output

Extract the profile as JSON matching `schemas/professor_profile.json`.

For `exam_format`:
- `duration_hours`: number of hours (may be fractional).
- `word_limit`: total word cap if specified; 0 if none.
- `open_book`: boolean.
- `structure`: array of parts with weight/type/count. If the memos describe a
  fixed structure (e.g., "Part I: 10 MC questions; Parts II–IV: three issue-
  spotter essays"), list each part. Otherwise leave empty array.
- `prompt_conventions`: verbatim quotes of conventions the professor always
  uses (e.g., a closing-sentence boilerplate).

For `pet_peeves`: one item per distinct peeve. Pet peeves are behaviors that
lose points even when the substantive analysis is correct (e.g., hedging,
misnamed future interests, voice mismatch).

For `favored_framings`: short phrases describing doctrinal lenses the
professor teaches — e.g., "Numerus clausus — the menu of estates is closed,"
"Penn Central three-factor balancing as the default for regulatory takings."

For `stable_traps`: scenarios the professor likes to re-test year over year.
Each trap has a short slug name and a 1-sentence `desc`.

For `voice_conventions`: if the professor cares about which voice the answer
adopts (law clerk memo vs advocate vs brief), capture that here.

For `commonly_tested`: short list of doctrines that appear in most exams.

For `source_artifact_paths`: echo the `path` values from `memo_sources` (plus
the syllabus path when provided).

Return JSON only.
