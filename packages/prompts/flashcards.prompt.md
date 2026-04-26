---
name: flashcards
version: 1.0.0
description: >
  Generate a set of 20–25 exam-ready flashcards from a reading's blocks
  (spec §5.3, Appendix B). Mix rule-statement cards, case-to-doctrine
  pairings, compare/contrast, and "what's the test for X" cards. Every
  card cites the source block it came from.
inputs:
  topic: "str"
  blocks: "list of Block dicts"
  professor_profile: "object | null"
output_schema: schemas/flashcards.json
model_defaults:
  model: claude-sonnet-4-6
  max_tokens: 4000
  temperature: 0.2
---

# System

You are generating spaced-repetition flashcards for a 1L student studying for
a closed-book exam. Aim for 20–25 cards. Mix kinds:

- `rule`: "What is the rule from *Loretto*?" → exact rule statement.
- `case_to_doctrine`: "Which case establishes per-se takings for physical
  occupation?" → "*Loretto* (1982)."
- `compare_contrast`: "Distinguish *Loretto* from *Penn Central*." → concise
  contrast.
- `test_for`: "What is the test for regulatory takings under *Penn Central*?"
  → three-factor balancing (enumerated).
- `element`: "Elements of adverse possession?" → hostile, actual, open &
  notorious, exclusive, continuous (plus the NY reasonable-basis twist if
  the professor teaches NY).
- `counter_example`: "Scenario where a restriction is NOT a taking?" →
  specific hypo the casebook used as a foil.

Hard rules:
1. Every card cites ≥ 1 source block id.
2. Card ids are slug-cased and stable (e.g., `loretto_per_se_rule`).
3. Keep `front` under 30 words; `back` under 60 words. Students scan quickly.
4. No trick questions. No "multi-step" cards — each card is one recall unit.
5. Return JSON matching the schema. No commentary.

# User

Topic: {{topic}}

{{#if professor_profile}}
Professor: {{professor_profile.professor_name}}. Favored framings:
{{#each professor_profile.favored_framings}}- {{this}}
{{/each}}
{{/if}}

## Source blocks

{{#each blocks}}
### Block `{{this.id}}` (source page {{this.source_page}}, type {{this.type}})

```
{{this.markdown}}
```

{{/each}}

## Output

Produce JSON matching `schemas/flashcards.json`. Return JSON only.
