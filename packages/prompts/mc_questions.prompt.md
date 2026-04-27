---
name: mc_questions
version: 1.0.0
description: >
  Generate a set of 10 multiple-choice questions with full explanations
  (spec §5.12). Pollack Part I is 10 MCs — this mirrors that format.
inputs:
  topic: "str"
  blocks: "list of Block dicts from the relevant reading"
  professor_profile: "object | null"
  num_questions: "int (default 10)"
output_schema: schemas/mc_questions.json
model_defaults:
  model: claude-sonnet-4-6
  max_tokens: 64000
  temperature: 0.2
---

# System

Generate realistic MC questions at the difficulty of a 1L doctrinal exam.
Pollack's MC style tests rule recall + subtle application — not trivia.

Hard rules:
1. Each question has exactly 4 options (A/B/C/D), one correct.
2. Distractors must be plausible. A "none of the above"-style filler
   option is disqualifying.
3. `explanation` explains WHY the correct answer is correct and what rule
   it invokes.
4. `distractor_explanations` has an entry for each of the three wrong
   letters explaining why it's wrong (one sentence each).
5. `doctrine_tested` is a short tag the student can use to group weakness
   areas.
6. If professor_profile provided, at least 2 questions test its
   `stable_traps`.
7. Return JSON matching the schema. No commentary.

# User

Topic: {{topic}}
Target count: {{num_questions}}

## Source blocks

{{#each blocks}}
### Block `{{this.id}}` (p. {{this.source_page}})

```
{{this.markdown}}
```

{{/each}}

{{#if professor_profile}}
## Traps to exercise
{{#each professor_profile.stable_traps}}- {{this.name}}: {{this.desc}}
{{/each}}
{{/if}}

## Output

Produce JSON matching `schemas/mc_questions.json`. Return JSON only.
