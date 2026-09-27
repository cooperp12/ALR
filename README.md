# ALR legacy compatibility - authoritative codename enrichment + anomaly-aware chronology

Extract into a new folder and run:

```cmd
setup-ALR.cmd
run-ALR.cmd
```

Press **1** to run Q1 -> Q2 -> Q3 and then solve Q6 from their evidence-validated sequence state.

## Investigation flow

```text
validated prompt/prior facts
        ↓
typed sequence-state facts + provenance
        ↓
historical CloudTrail identity evidence
        ↓
shared relationship/timeline graph
        ↓
adaptive RunInstances search
        ├── parsed route healthy → continue
        └── parsed=0 / raw>0
                    ↓
             anomaly Worker
                    ↓
             anomaly Supervisor
                    ↓
          Python raw-route failover
        ↓
complete-population chronological FIRST
        ↓
Python chronology invariant
        ↓
independent raw min(_time) boundary verification
        ↓
exact AMI + region
        ↓
tiered external Ubuntu release evidence
        ↓
version-derived direct official Ubuntu pages
        ↓
official two-word codename evidence
        ↓
canonical release
```

The Worker/Supervisor are **exception reviewers only**. They cannot override Python identity/chronology/verification gates or release a benchmark answer.

## Codename resolution

ALR legacy compatibility does not contain a release/codename table. Once external evidence has established an Ubuntu version, it constructs official Ubuntu release URLs from that version and fetches those pages directly. Search-engine discovery is only a fallback and is restricted to official Ubuntu hosts.

## Setup progress

`setup-ALR.cmd` prints each pytest test name and percentage. The large chronology regression is now bulk-seeded for test purposes, so setup should no longer appear stuck immediately after the earlier 133-test point.

## Fresh benchmark isolation

Each option-1 run archives prior mutable ALR legacy compatibility state to `ALR_benchmark_archive/<timestamp>/` before the benchmark begins. Previous evidence is preserved for inspection but cannot silently influence the fresh run.

## Benchmark contamination boundary

Benchmark question JSON contains questions and metadata only. Runtime contains no local Q6 answer/codename catalogue. Automatic scoring occurs only after investigation through an isolated normalized-answer SHA-256 oracle that is never passed to models or investigation tools.

## Diagnostics

Useful files after option 1:

- `ALR_sequence_summary.json`
- `ALR_q6_stage_trace.json`
- `ALR_q6_resolution.jsonl`
- `ALR_eval_runs.jsonl`
- `ALR_timeline.jsonl`
- `ALR_relationships.jsonl`
- `ALR_benchmark_run.json`

## Validation

183 automated tests pass in the development tree, including chronology, raw failover, independent verification, anomaly review, randomized literal-independence, benchmark isolation, relationship provenance, and direct-official codename-resolution tests.


## ALR v0.3.1
Added agentic-web-enrichment capability.
