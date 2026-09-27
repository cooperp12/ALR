---
name: result_semantics_recovery
description: Resolve ambiguous evidence through narrow capability-specific investigator contracts under supervisor control, with ALR legacy compatibility durable observations and measurement contracts.
version: 8
---

# RESULT SEMANTICS RECOVERY — ALR legacy compatibility

## Purpose

Resolve evidence gaps without giving one model a giant multi-purpose action schema.
Machine results are promoted into the durable evidence/observation store before either
agent continues. A later control-model failure must not erase analytical progress.

Never use known lab answers, scorer output, remembered challenge answers, or hidden
expected values.

## ALR legacy compatibility architecture

1. Tools produce machine evidence.
2. Python records raw evidence and extracts immutable `OBSERVED` facts.
3. For ranking questions, alternative complete measurements become measurement candidates.
4. Each complete measurement receives a stable `measurement_id`.
5. The formal `measurement_semantics_resolution` control skill selects only one existing
   `measurement_id` plus a reason and confidence. It cannot return fields, support IDs, SPL or a winner.
6. Python reconstructs all field/provenance/support IDs and freezes that judgement as a measurement contract.
7. Python derives the maximum/winner set mechanically. Genuine ties remain ties.
8. A grouped-pair check independently recomputes a supported distinct calculation.
9. The Supervisor controls remaining recovery routes, validation release and stop permission.

Timeline, relationship and ranking views are projections over machine evidence; they are not
separate case memories.

## Capability contracts

### semantic_field_review

Judge only whether the current field/representation matches the concept asked about.
Do not write SPL, choose a candidate, expand scope, or stop the investigation.

### schema_inspection

Return one focused same-scope inspection query or `DEFER`. Use it to obtain representative
events/fields needed for semantic judgement.

### alternative_aggregation

Return one materially different same-scope aggregation or `DEFER`. When it yields complete
`dc(...) AS ... BY ...` data, Python stores those measurements as candidate representations.

### temporal_pattern_analysis

Return a deterministic analysis request or `DEFER`. Temporal output is a view of durable
case evidence; aggregate facts do not need timestamps to remain first-class observations.

### relationship_analysis

Return one same-scope relationship/pivot query or `DEFER`. It must add evidence rather than
repeat an existing aggregation.

### scope_expansion

Return one genuine population/source expansion proposal or `DEFER`. A separate scope guard
reviews it. Scope expansion is never a generic tie-breaker.

## Measurement-contract rule

After a measurement contract is active, later distinct-ranking queries may not silently
switch to another distinct expression or group. Python blocks metric drift. Reopening
measurement semantics requires an explicit future state transition, not ordinary recovery.

## Candidate rule

Once a contract exists, Candidate Assessment is deterministic Python. The model is not asked
to rediscover the winner. `TIED_MAXIMUM` remains a tie; lexicographic ordering may format a
winner set but must never manufacture a single winner.
