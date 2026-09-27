---
name: bidirectional_timeframe
description: Build and reuse an evidence-backed event map around a versioned T0 anchor, including before/after positioning and entity relationships.
version: 3
---

BIDIRECTIONAL TIMEFRAME / CASE-MAP ENGINE

Use this only when surrounding temporal context materially helps answer the question.

1. MAP FIRST
   - Query the persistent case map before spending a new Splunk call.
   - Reuse prior mapped events/entities/relationships where relevant.

2. ESTABLISH EVIDENCE-BACKED T0
   - T0 must be a timestamp already present in stored timeline evidence.
   - Call `set_timeline_anchor` with event_time plus a reason and, where useful,
     evidence/entity information.
   - The engine rejects guessed/unmapped timestamps.
   - Changing T0 creates a new anchor version; prior anchor history is preserved.

3. SEARCH BIDIRECTIONALLY
   - Start with `timeline_before` and `timeline_after` at +/-30 minutes.
   - Pivot using `timeline_related` and `timeline_entities`.
   - If coverage remains incomplete, widen to +/-2 hours, then +/-24 hours.
   - Do not widen automatically when the narrow map already proves the answer.

4. RELATIVE POSITIONING
   - Timeline reads calculate `BEFORE`, `ANCHOR`, or `AFTER` and
     `seconds_from_anchor` against the current anchor.
   - `timeline_between` supports absolute time windows when needed.

5. ENTITY RELATIONSHIPS
   - The engine records normalized entities such as access keys, users, IPs, hosts,
     domains, URLs, resources, regions, and actions.
   - It records useful co-occurrence relationships such as USED_BY, SEEN_FROM,
     PERFORMED, CALLED, TARGETED, HOSTED_BY, and evidence-derived credential-to-credential relationships when two credentials occur in the same source event.

6. PROVENANCE
   - Preserve case ID, evidence ID, source/sourcetype, event type, query, method,
     event time, anchor version, and original compact event text.

7. STOP
   - Stop when the actual question is proven with sufficient checked evidence.
   - Do not invoke a full timeline workflow for a direct lookup or simple aggregation.

## ALR legacy compatibility relationship semantics
- Relationship edges carry confidence, a numeric confidence score, evidence provenance, and whether an edge is sufficient for identity expansion.
- Same-event explicit credential derivation and CloudTrail access-key -> IAM-user bindings are strong identity edges.
- IP/user-agent co-occurrence is supporting evidence only and must not independently widen actor identity.
- Temporal scopes should expand adaptively from evidence-backed anchors; do not assume one fixed +/- window.
