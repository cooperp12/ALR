# ALR legacy compatibility executable timeline framework

Map-first flow:

1. Query existing case-map rows/entities/relationships.
2. If needed, retrieve one targeted anchor event from Splunk.
3. Set/version T0 using a timestamp already present in evidence.
4. Inspect BEFORE/AFTER +/-30m.
5. Pivot on discovered entities and relationships.
6. Widen to +/-2h and +/-24h only when evidence remains incomplete.
7. Preserve anchor history and provenance; do not rewrite old evidence.

The engine calculates relative position at read time against the current anchor, so
anchor changes do not destroy historical event records.


## ALR legacy compatibility evidence-store relationship

The timeline is a projection over durable machine evidence, not the canonical evidence store. Non-temporal observations remain available to other views and recovery stages even when they do not appear on the timeline.
