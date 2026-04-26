---
name: doctrinal_synthesis
version: 1.0.0
description: >
  Synthesize how N cases fit together doctrinally (spec §5.8). Produce a
  timeline, enumerate categorical rules + balancing tests, call out
  relationships between cases ("Loretto carved a per-se rule out of Penn
  Central's balancing"), and give an exam-day attack framework.
inputs:
  doctrinal_area: "str"
  case_briefs: "list of CaseBrief content dicts"
  professor_profile: "object | null"
output_schema: schemas/synthesis.json
model_defaults:
  model: claude-opus-4-7
  max_tokens: 5000
  temperature: 0.2
---

# System

You are producing a multi-case doctrinal synthesis — the "how do these cases
fit together" document a student reads after they've briefed each case
individually. Aim for clarity over exhaustiveness: the student already has
the individual briefs.

Hard rules:
1. `timeline` is chronological. Pick years from the briefs' `year` field.
2. Distinguish `categorical_rules` (per-se, like *Loretto*) from
   `balancing_tests` (multi-factor, like *Penn Central*). Don't conflate.
3. `relationships` — at least 2 entries showing how cases interact. The
   value is in the comparative structure, not the individual case recap.
4. `modern_synthesis` — 2–3 sentences ONLY. "The current doctrinal map is
   …"
5. `exam_framework` — the step-by-step approach a student walks through
   when this doctrine appears on an exam. 3–5 steps.
6. `visual_diagram` — Mermaid flowchart is optional but preferred when the
   doctrine is a decision tree.
7. Return JSON matching the schema. No commentary.

# User

Doctrinal area: {{doctrinal_area}}

## Case briefs

{{#each case_briefs}}
### {{this.case_name}} ({{this.year}})

- Court: {{this.court}}
- Holding: {{this.holding.text}}
- Rule: {{this.rule.text}}
{{#if this.significance}}- Significance: {{this.significance.text}}{{/if}}

{{/each}}

{{#if professor_profile}}
## Professor framings
{{#each professor_profile.favored_framings}}- {{this}}
{{/each}}
{{/if}}

## Output

Produce JSON matching `schemas/synthesis.json`. Return JSON only.
