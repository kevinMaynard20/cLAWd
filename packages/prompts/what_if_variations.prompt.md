---
name: what_if_variations
version: 1.0.0
description: >
  Generate N fact-variations on a case (spec §5.10). Each variation
  changes one material fact, names the consequence, explains why
  doctrinally, and tags which doctrine the variation tests.
inputs:
  case_brief: "CaseBrief content dict (facts, holding, rule, reasoning)"
  num_variations: "int (default 5)"
  professor_profile: "object | null"
output_schema: schemas/what_if_variations.json
model_defaults:
  model: claude-opus-4-7
  max_tokens: 3000
  temperature: 0.4
---

# System

You are generating N fact-variations on a case to probe the student's
understanding. Each variation should flip the outcome or substantially
shift the analysis — not trivial wording tweaks.

If the brief below is empty, mostly placeholder text, or marked
`from_general_knowledge`, fall back to your own knowledge of the named
case to produce real variations. Do not produce variations that just
restate "no information available" — generate substantive alternates.

Hard rules:
1. Each variation changes ONE fact (not three at once).
2. `consequence` explains how the outcome changes — ruling, remedy, who
   wins. Concrete.
3. `doctrinal_reason` cites the rule/element/exception that drives the
   different outcome.
4. `tests_understanding_of` names the distinction the variation is probing
   (e.g., "permanent vs temporary physical invasion").
5. If professor_profile provided, align at least one variation with a
   stable_trap they teach.
6. Return JSON matching the schema. No commentary.

# User

Case: {{case_brief.case_name}}

- Facts: {{#each case_brief.facts}}{{this.text}} {{/each}}
- Holding: {{case_brief.holding.text}}
- Rule: {{case_brief.rule.text}}

Number of variations: {{num_variations}}

{{#if professor_profile}}
## Stable traps from professor
{{#each professor_profile.stable_traps}}- {{this.name}}: {{this.desc}}
{{/each}}
{{/if}}

## Output

Return ONE JSON object matching this shape, no commentary:

```
{
  "case_name": "<exact case name from input>",
  "variations": [
    {
      "id": "v1",                            // sequential slug, v1..vN
      "fact_changed": "<the single fact that's different>",
      "consequence": "<how the outcome changes — concrete>",
      "doctrinal_reason": "<rule/element/exception driving the change>",
      "tests_understanding_of": "<the doctrinal distinction probed>"
    }
    // ... continue for the requested number of variations
  ],
  "sources": []      // empty array is fine; lineage tracked separately
}
```

Every variation MUST include all five fields including `id`. `case_name`
at the top level MUST match the input case name exactly. `sources` may be
empty.
