---
name: json_nested_fields
description: Extract nested JSON values such as requestParameters, responseElements, or userIdentity safely and compactly.
version: 1
---

NESTED JSON / STRUCTURED FIELDS
- Prefer already-extracted fields first.
- If nested JSON is only present in _raw, use spath or a targeted rex.
- For CloudTrail targets, inspect requestParameters and the error message/event
  together rather than assuming the resource name from nearby events.
- Return only the nested keys needed to answer the question.
