---
name: splunk_query_efficiency
description: Keep Splunk queries targeted and reduce results before they reach the LLM context window.
version: 1
---

SPLUNK QUERY EFFICIENCY
- Filter early by index, sourcetype, known identifiers, fields, and event/action.
- Prefer stats, table, fields, dedup, sort, head, rex, and spath.
- Do not sample arbitrary raw BOTSv3 events with bare index=botsv3 or
  index=botsv3 | head N.
- If the sourcetype is genuinely unknown, discover it efficiently with something
  like: index=botsv3 <target terms> | stats count by sourcetype | sort - count
- Ask Splunk to return the small fields needed for the decision rather than
  large _raw payloads.
