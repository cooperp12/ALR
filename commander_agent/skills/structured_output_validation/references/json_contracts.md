# JSON contracts

ALR treats JSON as a typed interface, not as arbitrary model text.

- Search arguments: object; `query` is a non-empty string; optional count/time/format
  fields use their documented scalar types.
- Search result: object; if `events` or `results` exists it is an array; an `error`
  object is classified as a failed search and is not cached as evidence.
- Planner/reviewer output: required keys and enums are validated before use.
- Persistent records: must survive a strict JSON dump/load round trip.
