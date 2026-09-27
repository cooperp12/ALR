# ALR legacy compatibility implementation

ALR legacy compatibility is a targeted enrichment/reliability iteration based on the live ALR run and its generated artifacts. ALR successfully proved the evidence-derived identity chain, temporary credential pivot, timeline anchor, raw route failover, true chronological FIRST event, independent raw boundary verification, exact AMI/region, and Ubuntu release version. It stopped only because the evidenced release version could not be connected to one official two-word codename.

## 1. Preserve the proven ALR investigation path

ALR legacy compatibility deliberately does not redesign Q6. It retains:

- validated Q1 -> Q2 -> Q3 sequence-state reuse;
- evidence-derived IAM user and temporary credential relationships;
- confidence/provenance on relationship edges;
- evidence-derived timeline anchor;
- parsed-route health detection and immediate raw fallback;
- adaptive temporal expansion and stability checks;
- complete-population chronological FIRST selection;
- independent raw `min(_time)` boundary verification;
- anomaly-only Investigator/Worker and Supervisor reviews;
- exact `AMI + region` requirements;
- fail-closed canonical release gating.

## 2. Deterministic official release -> codename route

The live ALR run had already externally established an Ubuntu release version, but its codename resolver depended primarily on search-engine discovery. ALR legacy compatibility instead derives official URLs from the evidenced version itself:

```text
externally evidenced Ubuntu version
        ↓
construct official version URLs
        ├─ releases.ubuntu.com/<version>/
        ├─ cloud-images.ubuntu.com/releases/<version>/release/
        └─ ubuntu.com/<version-with-dash>
        ↓
fetch official page text
        ↓
extract explicit two-word codename from the same version statement
        ↓
require agreement across multiple successful official pages
        ↓
release candidate
```

The runtime contains only URL construction and structural parsing rules. It does **not** contain a version -> codename dictionary or the benchmark answer.

If direct official pages do not yield evidence, ALR legacy compatibility falls back to search restricted to official Ubuntu domains.

## 3. Conflict handling

If two reachable official pages produce different codenames for the same evidenced version, ALR legacy compatibility returns unresolved evidence rather than picking one. The anomaly layer may diagnose the problem, but Python cannot waive this invariant.

## 4. Better enrichment diagnostics

Successful codename resolution records:

- source URL;
- source tier (`official_direct_release_page` or `official_search_fallback`);
- corroborating official sources;
- source count in the stage trace.

Failure packets record the deterministic official URLs that were attempted before fallback.

## 5. Setup-test progress and stall fix

The ALR setup was interrupted after 133 tests because the next chronology regression seeded a large timeline through normal append and relationship-ledger writes. That test was proving chronology semantics, not append throughput.

ALR legacy compatibility keeps the >100-event regression but bulk-seeds the test timeline so Windows filesystem overhead does not dominate setup. The setup script also invokes pytest with verbose per-test progress and percentage plus slow-test reporting. A slower test is therefore visible rather than appearing frozen.

## 6. Contamination boundary

- No local Ubuntu release/codename answer catalogue.
- No live Q6 AMI literal or plaintext benchmark answer in runtime/tests/docs.
- Synthetic codename fixtures are used for generalisation tests.
- Benchmark question JSON contains no expected outputs or expected patterns.
- Scoring remains post-investigation through the isolated normalized-answer SHA-256 oracle.
- The oracle is never sent to the Investigator, Supervisor, sequence state, Splunk, timeline engine, or external enrichment code.

## 7. New regression coverage

ALR legacy compatibility adds tests proving that:

- official codename URLs are constructed solely from the evidenced version;
- direct official release pages are preferred over search;
- multiple official direct sources can corroborate one synthetic codename;
- conflicting direct official codenames fail closed;
- the >100-event chronology regression remains intact without the slow append-heavy fixture.

## Validation

183 automated tests pass in the development tree before packaging. The package is also contamination-scanned and then retested from the exact extracted ZIP before release.
