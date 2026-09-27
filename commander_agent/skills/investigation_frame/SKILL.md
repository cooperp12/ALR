---
name: investigation_frame
description: Convert a question into source/scope/actor/event/measure/output concepts before selecting physical fields.
---
# Investigation Frame

Interpret the investigative intent before writing SPL or selecting schema fields.

Separate:
- source/domain concept;
- scope/population conditions;
- actor/entity concept;
- event/action concept;
- measurement concept and semantic role;
- aggregation/ranking;
- temporal constraint;
- requested output;
- whether reference-following or external enrichment is required.

Do not answer the question and do not encode benchmark-specific values.
A field used to detect/filter failed events may be different from the field whose values are measured.
