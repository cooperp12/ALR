---
name: external_enrichment
description: Follow a public URL or research an identifier such as an AMI when Splunk alone cannot provide the final semantic answer.
version: 2
---

EXTERNAL ENRICHMENT
- First obtain the external indicator from Splunk evidence: e.g. URL, AMI/image ID,
  package/version identifier, or another concrete artefact.
- Only then use web_search or fetch_url.
- Prefer deterministic direct/authoritative URLs derived from already-evidenced identifiers before search-engine discovery.
- Keep the external evidence tied to the exact artefact found in Splunk.
- Do not search for or guess the expected lab answer directly.
