---
name: spl_distinct_count
description: Bootstrap most-distinct investigations with reducing Splunk aggregation while ALR legacy compatibility resolves measurement semantics explicitly.
version: 6
---

# SPLUNK DISTINCT COUNT — ALR legacy compatibility

- Distinguish total event count from distinct-value count.
- For wording such as `most distinct X`, bootstrap with `dc(<candidate_field>)` grouped by
  the requested entity, but treat that field as a provisional measurement hypothesis.
- Error/failure concepts may have code/category/message/composite representations at different
  granularity. Recovery should collect materially different complete measurements and
  representative events.
- Python records each complete `dc(...) AS alias BY group` result as a durable measurement
  candidate. A semantic model chooses the representation; Python chooses the winner(s).
- Status-like sentinels (`success`, `ok`, `none`, `null`, `n/a`, `passed`, `-`) must not
  increase an error count.
- Preserve top ties. Never use unrelated event frequency or lexical ordering as a tie-breaker.
- `values(<field>)` is useful for inspection but is not automatically independent validation.
- After a measurement contract is active, a later ranking query that changes the contracted
  distinct expression/group is blocked as semantic metric drift.
