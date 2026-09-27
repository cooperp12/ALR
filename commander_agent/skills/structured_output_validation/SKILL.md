---
name: structured_output_validation
description: Validate JSON/tool/query objects before they are trusted, cached, or persisted; reject malformed/error objects instead of turning them into evidence.
version: 2
---

STRUCTURED OUTPUT VALIDATION

Use this as a harness-level safety/reliability skill on every investigation.

1. Before persisting JSON/JSONL, prove that the object is JSON-serialisable and
   round-trips through JSON without relying on `default=str`.
2. Before executing or caching a search request, validate that it is an object with
   a non-empty string `query` and correctly typed optional parameters.
3. Before treating a Splunk result as evidence, parse it as JSON and reject objects
   that contain an `error` field or malformed `events/results` structures.
4. Model-produced planner/reviewer JSON must satisfy the expected object contract;
   malformed output falls back to deterministic/local logic instead of being saved.
5. A failed/malformed search is failure telemetry, not successful cached work.
6. Recursively inspect nested error/detail/message objects. Classify authentication
   and authorisation failures (for example Unauthorized/Forbidden) as non-retryable
   infrastructure failures; they must stop the search path rather than consume LLM
   retry rounds.
7. Never silently repair semantics. Syntax-only repair is allowed only when the
   repaired object passes the same schema validation afterwards.
