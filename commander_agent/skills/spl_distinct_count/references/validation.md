# Distinct-count validation

Primary evidence should use `stats dc(field)` grouped by the requested entity.
An independent check should not repeat `dc()` with cosmetic changes. Prefer a
second method such as `values(field)` and compare the unique values for the same
group. For ALR, the harness treats these as different method classes and will
not count two `dc()` variants as independent support.
