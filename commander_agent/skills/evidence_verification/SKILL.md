---
name: evidence_verification
description: Verify the final answer against every material condition in the question and avoid selecting a merely nearby event.
version: 1
---

EVIDENCE VERIFICATION
Before answering:
1. Identify every material constraint in the user's question.
2. Confirm the chosen event/result satisfies each constraint.
3. Confirm the requested output field comes from that same matching evidence.
4. If multiple candidate events exist and the wording is ambiguous, run one
   narrower confirmation query.
5. Never conclude that data is absent based only on an unrelated broad sample.
