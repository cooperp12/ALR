---
name: incident_sequence_state
description: Reuse evidence-validated facts from earlier questions in the same investigation sequence.
version: 2
---

INCIDENT SEQUENCE STATE
- Promote only findings that passed the normal evidence-validation gate.
- Store typed facts (role, value, question, support IDs, evidence provenance), not only prior answer prose.
- Any later question may consume relevant facts; this is not Q6-specific.
- Prompt-provided facts are legitimate constraints and do not need rediscovery.
- Scorer expected values/oracles are never sequence facts.
- Contradictory machine evidence blocks release rather than silently overwriting validated state.
- Preserve time/provenance in supporting evidence so later timeline skills can establish attack scope.
