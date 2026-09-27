---
name: scope_invariant_guard
description: Validate an explicit scope-change request before an investigation query may remove current scope constraints.
version: 1
---

# SCOPE INVARIANT GUARD

## Purpose

Prevent an investigation from silently changing the population it is supposed to
answer about. The current scope contract is derived from the active investigation
strategy/query, not from a hard-coded lab answer.

A proposed query that preserves the current scope does not need this skill.
A proposed query that removes or changes a current invariant must declare the change,
explain why the broader/different population is necessary, and be reviewed here before
execution.

## Review principles

- Compare the original question, current scope contract, proposed query, and stated
  justification.
- Do not allow a change merely because it returns more rows or breaks a tie.
- A scope change is useful only when the original question can still be answered from
  the changed population without conflating unrelated events/entities.
- Prefer changing measurement, field granularity, aggregation method, temporal view,
  or schema inspection while preserving scope.
- If a scope expansion is genuinely necessary for context, require the eventual answer
  to remain tied back to the original scoped population.
- Never use scorer/expected-answer knowledge.

Return a structured ALLOW or DENY decision with a short reason.
