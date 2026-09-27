---
name: cloudtrail_identity_expansion
description: Expand an evidence-validated AWS access key into the IAM principal and temporary credentials that are demonstrably related in CloudTrail.
version: 1
---

# CloudTrail Identity Expansion

Use this when a question refers to the same compromised IAM user or actor, but later API calls may not reuse the original long-term access-key literal.

## Method

1. Start from an evidence-validated credential or principal. Do not start from an expected answer.
2. Query historical CloudTrail for that exact credential and preserve raw events.
3. Derive stable identity attributes from those events, especially `userIdentity.userName`, ARN, account ID and principal ID.
4. Inspect successful credential-minting events such as `GetSessionToken` for `responseElements.credentials.accessKeyId` and treat those temporary access keys as related only when the relationship appears in the same source event.
5. Pivot subsequent activity on the resolved IAM username and the original/derived access keys together.
6. Keep the relationship evidence and query provenance. A later event matches the actor only if at least one evidence-derived identity binding matches.
7. If the original credential maps to multiple IAM usernames, stop for semantic review rather than choosing one.

## Guardrails

- Do not assume all activity by an AWS account belongs to the compromised user.
- Do not treat temporal proximity alone as an identity relationship when a username or credential relationship is available.
- Do not hard-code incident-specific usernames, temporary keys, IPs, AMIs, or answers into the skill.
- For BOTSv3 historical data, use `earliest=0`.
