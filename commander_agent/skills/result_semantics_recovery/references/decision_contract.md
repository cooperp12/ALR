# Decision contract

The result-semantics skill owns interpretation and recovery choice. The orchestrator
owns only execution mechanics.

Allowed execution decisions that can carry `recommended_spl`:

- RUN_CHECK_QUERY
- RUN_ALTERNATIVE_QUERY
- EXPAND_SCOPE
- INVESTIGATE_SCHEMA

ACCEPT may be promoted to a structured finding only when `candidate_status` is
`supported`, `candidate` is non-empty, and the referenced support IDs exist. This is a
contract check, not a domain-specific interpretation.
