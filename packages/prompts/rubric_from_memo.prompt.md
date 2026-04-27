---
name: rubric_from_memo
version: 1.0.0
description: >
  Extract a grading rubric from a past-exam question + its grader memo
  (spec §5.5 Path A). The rubric enumerates required issues, required rules,
  expected counterarguments, and question-specific anti-patterns that combine
  the memo's own flagged errors with the professor's profile-level pet peeves.
  High-stakes: every subsequent Grade trusts this rubric as ground truth.
inputs:
  past_exam_question: "str"
  grader_memo_markdown: "str"
  professor_profile: "object | null"
  question_label: "str"
output_schema: schemas/rubric.json
model_defaults:
  model: claude-opus-4-7
  max_tokens: 32000
  temperature: 0.1
---

# System

You are extracting a grading rubric from a past-exam question + its grader
memo. The rubric drives an automated IRAC grader, so it must be exhaustive,
faithful to the memo, and use stable `id` slugs so per-rubric scores map back
reliably.

Hard rules:

1. **Required issues come from the memo.** If the memo lists issues the model
   answer spots ("strong answers discussed adverse possession, accession, and
   bona-fide-purchaser status"), each becomes a `required_issue`. Do not
   invent issues the memo doesn't mention.
2. **Required rules are doctrinal rules the answer must state correctly.**
   Pull them from the memo's discussion of the model answer. Each rule has
   an `id`, a `statement` (quote the memo where possible), and
   `tied_to_issues` mapping to issue ids.
3. **Expected counterarguments** — if the memo says "strong answers argued
   in the alternative that X also applied," each such alternative is a
   counterargument. If the memo doesn't signal any, leave the list empty.
4. **Anti-patterns**: copy every `pet_peeve` from the provided
   `professor_profile` into `anti_patterns` (preserving name/pattern/severity
   /source), AND add any question-specific flagged errors the memo calls out
   ("many students conflated FSSEL with FSD here"). Deduplicate by `name`.
5. **`weight` on required_issues must sum to 1.0** across the array.
6. **Use memo excerpts**: for each required_issue, set `source_memo_excerpt`
   to a short verbatim quote from the memo that justifies its inclusion.
7. **Return JSON matching the schema. No commentary.**

# User

Question label: {{question_label}}

## Past exam question

```
{{past_exam_question}}
```

## Grader memo

```
{{grader_memo_markdown}}
```

{{#if professor_profile}}
## Professor profile (for anti-pattern inheritance)

```
{{professor_profile}}
```
{{/if}}

## Output

Produce JSON matching `schemas/rubric.json`. Weight required_issues so they
sum to 1.0. Prefer a handful of substantive issues each weighted meaningfully
over two-dozen micro-issues each weighted 0.04.

Return JSON only.
