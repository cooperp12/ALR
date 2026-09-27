---
name: chronological_first_event
description: Correctly identify the first/earliest attempt by explicit chronological ordering.
version: 1
---

FIRST / EARLIEST EVENT
- Do not assume Splunk's default result order proves which attempt happened first.
- Explicitly sort chronologically ascending, for example:
    | sort 0 + _time
    | head 1
- Preserve eventTime/_time plus the identifying fields needed to verify the event.
