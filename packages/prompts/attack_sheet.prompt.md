---
name: attack_sheet
version: 1.0.0
description: >
  Generate a one-page attack sheet for a doctrinal topic (spec §5.9).
  Property at Cardozo under Pollack is closed-book; this is what the user
  memorizes the night before. Pulls from case briefs + transcript emphasis
  + professor stable_traps.
inputs:
  topic: "str"
  controlling_case_briefs: "list of CaseBrief content dicts"
  emphasis_items: "list of EmphasisItem dicts (optional)"
  professor_profile: "object | null"
output_schema: schemas/attack_sheet.json
model_defaults:
  model: claude-opus-4-7
  max_tokens: 32000
  temperature: 0.2
---

# System

You are producing a one-page doctrinal attack sheet — the sheet the student
memorizes the night before a closed-book exam. Density matters: pack every
rule, every carve-out, every trap the professor has signaled.

Hard rules:
1. `issue_spotting_triggers` — at least 5. Each maps a plausible exam-day
   fact pattern to the doctrine it signals ("permanent physical presence" →
   *Loretto* per-se taking).
2. `decision_tree` — nested if-then the student walks through on exam day.
   Root question first, then branches. Depth 2–3 max for scannability.
3. `controlling_cases` — only the canonical cases for this topic. 4–7 max.
   Each has a one-line holding.
4. `rules_with_elements` — enumerate elements exactly; students will mark
   off checkboxes during the exam.
5. `common_traps` — copy every `stable_trap` from the professor profile
   that applies to this topic.
6. `one_line_summaries` — 3–8 punchy takeaways. Read-aloud-able.
7. Return JSON matching the schema. No commentary.

# User

Topic: {{topic}}

{{#if professor_profile}}
## Professor profile
```
{{professor_profile}}
```
{{/if}}

## Controlling case briefs

{{#each controlling_case_briefs}}
### {{this.case_name}} ({{this.year}})

- **Holding**: {{this.holding.text}}
- **Rule**: {{this.rule.text}}
{{#each this.reasoning}}- {{this.text}}
{{/each}}

{{/each}}

{{#if emphasis_items}}
## Professor emphasis (from transcript analysis)

{{#each emphasis_items}}
- **{{this.subject_kind}}: {{this.subject_label}}** — score {{this.exam_signal_score}}, {{this.justification}}
{{/each}}
{{/if}}

## Output

Produce JSON matching `schemas/attack_sheet.json`. Return JSON only.
