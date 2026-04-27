---
name: hypo_generation
version: 1.0.0
description: >
  Generate a novel exam-style hypothetical AND its grading rubric
  simultaneously (spec §5.5 Path B). Rubric + hypo must be co-generated so
  every issue the hypo tests is represented in the rubric; this is how we
  guarantee rubric coverage without a separate reconciliation pass.
inputs:
  corpus_name: "str"
  topics_to_cover: "list of str"
  professor_profile: "object | null"
  source_blocks: "list of Block dicts | null"
  issue_density_target: "int"
output_schema: schemas/hypo.json
model_defaults:
  model: claude-opus-4-7
  max_tokens: 32000
  temperature: 0.4
---

# System

You are generating a novel exam-style hypothetical that tests the given
topics in the given course. The hypo must be tight, realistically-messy,
and — critically — must ship with a complete rubric that enumerates every
issue the hypo intentionally tests.

Hard rules:

1. **Rubric coverage is a guarantee, not a hope.** Every issue your hypo
   exercises must appear in `rubric.required_issues`. Rule statements the
   answer must get right appear in `rubric.required_rules`. Alternative
   arguments the student should argue in the alternative appear in
   `rubric.expected_counterarguments`.
2. **Match the professor's voice when provided.** Use the professor's
   favored_framings in how the fact pattern is structured; echo their
   pet_peeves into `rubric.anti_patterns` so the grader penalizes the same
   sins.
3. **Density target**: aim for `issue_density_target` distinct testable
   issues. If the professor profile specifies a density like "7–10 distinct
   issues per fact pattern," honor it.
4. **Write the hypo in the voice the student will be graded for** (law clerk
   memo, client's lawyer, brief) — put that voice in `rubric.prompt_role` AND
   in `hypo.role`. Wrong voice = lost points per §Appendix A.
5. **Ground in the source material when provided.** The `source_blocks`
   input is optional casebook context; use it to ensure the doctrines you
   test are actually covered in the student's assigned reading.
6. **Return JSON matching the schema. No commentary.**

# User

Corpus: {{corpus_name}}
Topics: {{#each topics_to_cover}}{{this}}, {{/each}}
Issue density target: {{issue_density_target}}

{{#if professor_profile}}
## Professor profile

```
{{professor_profile}}
```
{{/if}}

{{#if source_blocks}}
## Source context (casebook blocks)

{{#each source_blocks}}
— Block `{{this.id}}` (source page {{this.source_page}}):

```
{{this.markdown}}
```

{{/each}}
{{/if}}

## Output

Produce JSON matching `schemas/hypo.json`. `rubric` is the embedded Rubric
that matches `schemas/rubric.json`.

`rubric.question_label` should be a generated slug like `"takings_hypo_3"`.
`rubric.required_issues` must sum to weight 1.0. `rubric.prompt_role` must
match `hypo.role`. `rubric.anti_patterns` must include every profile pet
peeve verbatim, plus any extras the hypo's fact pattern opens the door to.

Return JSON only.
