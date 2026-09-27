---
name: measurement_semantics_resolution
description: Select the already-measured representation that best matches the user's requested conceptual quantity.
version: 1
---

# MEASUREMENT SEMANTICS RESOLUTION — ALR legacy compatibility

## Purpose

Resolve one narrow question:

> Which **existing complete machine measurement** best represents the conceptual quantity requested by the user?

This skill does **not** calculate a winner. It does not write SPL, change scope, invent a field,
choose a candidate, or decide when the investigation stops.

Python has already:

1. executed the searches;
2. preserved the raw machine evidence;
3. extracted immutable observations;
4. determined which aggregate measurements are complete;
5. assigned each complete measurement a stable `measurement_id`.

Your only job is semantic selection among those IDs.

## Output contract

Return exactly three fields:

```json
{
  "selected_measurement_id": "MEAS-E4-1234abcd",
  "reason": "Why this existing measurement best matches the user's requested concept.",
  "confidence": "high"
}
```

If no defensible selection exists, return:

```json
{
  "selected_measurement_id": null,
  "reason": "Why the existing measurements remain semantically ambiguous.",
  "confidence": "low"
}
```

Do not return:

- `evidence_id`;
- `support_ids`;
- `group_field`;
- `metric_field`;
- `source_expression`;
- SPL;
- a candidate/winner;
- a tie-break rule;
- a scope expansion.

Python reconstructs all of those from the selected `measurement_id`.

## Decision rules

- Select semantics because they match the wording and evidence, **never because they produce a unique winner**.
- A shared generic error code can be too coarse when representative events describe materially different failed operations.
- Exact error messages can be too fine when differences are only volatile IDs, timestamps, request IDs, or other incidental values.
- A composite operation/error representation is appropriate when the operation itself materially distinguishes the failure represented by the user's concept.
- Prefer the least transformed representation that preserves the distinctions the user actually asked about.
- Only select an ID that appears in the supplied complete-measurement list.
- If two representations are equally defensible and the evidence cannot distinguish them, return `null` rather than inventing a preference.

## Q3 semantic pattern

If one generic `AccessDenied` error code is reused across several materially different IAM operations,
then `dc(errorCode)` may collapse distinct failed actions into one category. If an existing composite
operation/error measurement preserves those materially different failures, it can be a more faithful
representation of "distinct errors" than the generic code alone.

That judgement must be based on representative machine evidence, not on which measurement happens to
produce the expected answer.
