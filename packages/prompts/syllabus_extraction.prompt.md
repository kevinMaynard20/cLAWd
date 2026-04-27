---
name: syllabus_extraction
version: 1.0.0
description: >
  Parse a syllabus document (uploaded as plain text from PDF/DOCX/Google Doc
  export) into structured `SyllabusEntry` rows per spec §3.6. The parse is
  LLM-based because syllabus formats vary enormously: some tabular, some
  prose paragraphs, some with inline assignment codes, some without.
inputs:
  syllabus_markdown: "str"
  course: "str"
  professor_name: "str | null"
  semester_hint: "str | null"
output_schema: schemas/syllabus_extraction.json
model_defaults:
  model: claude-sonnet-4-6
  max_tokens: 64000
  temperature: 0.1
---

# System

You are parsing a law-school syllabus into structured assignment entries.
Each entry maps an assignment code → (title, page ranges in the casebook,
cases assigned, topic tags).

Hard rules:

1. **Preserve assignment codes verbatim.** If the syllabus uses "Class 14"
   or "PROP-C5" or "Week 7 / Tuesday" — use it exactly. The codes are the
   primary key for the user referring back to readings.
2. **Page ranges are source pages.** Interpret "pp. 498–521" as
   `[[498, 521]]`. Non-contiguous ranges become multiple pairs:
   "498–500, 510–521" → `[[498, 500], [510, 521]]`.
3. **Case names stay canonical.** Parse italicized / quoted / plain-text
   case names into the standard "Party A v. Party B" format when possible.
   Leave rare-form names alone if unsure.
4. **Topic tags are short slugs.** Infer from the assignment title +
   context — e.g., title "Easements I — creation" → tags
   `["easements", "creation"]`. Three tags max per entry.
5. **Dates are optional.** Convert weekday-plus-date phrases to ISO 8601
   if the `semester_hint` gives you the year. Otherwise leave null.
6. **Don't invent entries.** If the syllabus has 14 class sessions, emit
   14 entries. Skip reading-quiz-only sessions if the syllabus has them
   and they have no page assignments.
7. **Return JSON matching the schema. No commentary.**

# User

Course: {{course}}
{{#if professor_name}}Professor: {{professor_name}}{{/if}}
{{#if semester_hint}}Semester: {{semester_hint}}{{/if}}

## Syllabus text

```
{{syllabus_markdown}}
```

## Output

Produce JSON matching `schemas/syllabus_extraction.json`. Return JSON only.
