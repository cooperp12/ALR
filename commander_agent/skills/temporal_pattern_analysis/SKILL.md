---
name: temporal_pattern_analysis
description: Deterministically analyse scoped event behaviour over time using Splunk reduction plus fast Python analytics.
version: 1
---

# TEMPORAL PATTERN ANALYSIS

## Purpose

Use deterministic analytics when the investigation benefits from trends, bursts,
changes, diversity growth, or comparisons over time. The LLM chooses *what* should be
measured. Splunk and Python perform the measurement.

This skill is not limited to failures. It can analyse any already-scoped event set:
authentication, CloudTrail actions, network events, DNS activity, process launches,
alerts, errors, successes, entities, or other event populations.

## Core architecture

1. Preserve the current investigation scope contract.
2. Reduce the specific scoped events in Splunk before moving data into Python.
3. Let Splunk choose its own time binning unless the investigation has an explicit
   temporal resolution requirement.
4. Analyse the reduced series with Polars and NumPy.
5. Produce machine-readable features for the LLM and graph artefacts for the human.
6. Never ask the LLM to estimate slopes, peaks, cardinality growth, or other numeric
   properties by visually reading rows when Python can compute them exactly.

## No answer-specific constants

Do not encode challenge answers, entity IDs, thresholds, expected winners, fixed time
windows, or domain-specific cut-offs. Measurements must come from the current scoped
data. Algorithmic computations may use mathematically defined quantities such as
median, MAD, linear regression, finite differences, correlations, and spectral power.

## Useful measures

The requesting reasoning skill may ask for any combination of:

- event volume over time;
- distinct-value count over time;
- cumulative distinct-value growth;
- newly observed distinct values per time bucket;
- first/last activity and active duration;
- trend slope;
- largest positive/negative change between adjacent buckets;
- median and median absolute deviation;
- strongest robust deviation from the series median;
- dominant periodic component and its relative spectral power;
- correlation between volume and distinct-value count;
- comparison of entities using a named computed feature.

The graph is a human aid. The LLM should reason from the structured feature output,
not from pixels.

## Query optimisation

Always reduce server-side first. The generated SPL must retain the current scope
contract and should return bucketed aggregates rather than raw events. The Python
engine must not export the whole BOTSv3 dataset merely to construct a chart.

## Relationship to semantic recovery

Temporal analysis is a recovery option, not a mandatory step for every question. Use
it when:

- the question is explicitly temporal;
- volume/diversity trends directly answer the question;
- a static aggregation is ambiguous and temporal behaviour can distinguish competing
  hypotheses;
- a schema/field choice can be tested by comparing its behaviour over time.

Do not use temporal analysis merely because data contains timestamps.
