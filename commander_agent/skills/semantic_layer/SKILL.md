---
name: semantic_layer
description: Resolve investigative concepts to source/schema roles, detect ambiguity, and invalidate coarse or contradictory bindings.
---
# Semantic Layer

Use domain tradecraft to resolve concepts to physical schema roles before query construction.

Principles:
- Resolve meaning before looking at winner/result values.
- Separate population/filter fields from measurement/output fields.
- Prefer role and granularity over lexical field-name similarity.
- Preserve provenance for every semantic binding.
- Detect one-to-many/multi-field relationships that show a selected representation is too coarse.
- Invalidate and reopen a binding when later evidence contradicts its semantic role.
- Skills encode tradecraft and workflows, never benchmark answers.
