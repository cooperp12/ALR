# ALR — Agentic Log Retrieval

https://img.shields.io/badge/Project-Agentic%20Log%20Retrieval-blue
https://img.shields.io/badge/Python-3.x-green
https://img.shields.io/badge/AI-Agentic%20Framework-purple

## Overview

**Agentic Log Retrieval (ALR)** is an evidence-driven AI security investigation framework designed to assist analysts with complex cybersecurity investigations by combining agentic reasoning with deterministic evidence validation.

ALR combines:

- Local Large Language Models (LLMs)
- Worker/Supervisor agent orchestration
- Security telemetry analysis
- Splunk-based investigation workflows
- Semantic investigation skills
- Evidence tracking and provenance
- Deterministic validation boundaries
- Agentic web enrichment
- Reproducible investigation workflows

The goal of ALR is to provide an investigation assistant that can reason over evidence while maintaining strict validation, provenance, and reproducibility controls.

AI agents assist with investigation decisions, but deterministic Python controls identity validation, chronology, evidence verification, and final answer release boundaries.

---

## Inspiration

ALR was inspired by the investigation workflows and AI-assisted security concepts demonstrated in the [BSides Canberra 2026 Copilot project](https://github.com/graphistry/bsides-canberra26-copilot).

That project provided practical examples and code patterns exploring:

- AI-assisted security analysis
- Investigation workflows
- Security telemetry reasoning
- Agent-based problem solving

ALR expands these concepts into an evidence-driven investigation framework focused on:

- Validated investigation state
- Provenance tracking
- Supervisor review loops
- Deterministic verification
- Reusable investigation skills

ALR is an independent project and is not a fork of the BSides Canberra Copilot project.

---

## Features

ALR provides:

- Agentic security investigation workflows
- Worker/Supervisor exception review model
- Splunk investigation integration
- AWS CloudTrail investigation support
- Timeline and chronology analysis
- Evidence provenance tracking
- Deterministic validation gates
- External evidence enrichment
- Local LLM support through Ollama
- Benchmark isolation and contamination controls
- AI platform safety and benchmark behaviour guidance
- Reproducible investigation workflows

---

## Architecture

ALR uses a layered investigation architecture:

```text
Investigation Request
        |
        v
Typed Investigation State
        |
        v
Worker / Supervisor Reasoning
        |
+---------------+---------------+
|                               |
v                               v
Investigation Skills       Evidence Validation
|                               |
+---------------+---------------+
                |
                v
    Deterministic Python Gates
                |
                v
 Security Data Sources
 (Splunk / APIs / Web Evidence)
```

---

## Worker / Supervisor Model

ALR uses Worker and Supervisor agents as **exception reviewers only**.

They assist when investigation judgement is required, including:

- Ambiguous evidence relationships
- Unexpected search results
- Chronology anomalies
- Recovery decisions

They cannot:

- Override Python identity validation
- Override chronology invariants
- Bypass evidence verification
- Release benchmark answers directly

The final investigation result must pass deterministic validation.

---

## Investigation Example: Legacy Compatibility Workflow

**Authoritative codename enrichment and anomaly-aware chronology**

Extract ALR into a new folder and run:

```text
setup-ALR.cmd
run-ALR.cmd
```

Press **1** to run **Q1 → Q2 → Q3** and then solve **Q6** using their evidence-validated sequence state.

### Investigation Flow

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
        |
        +── parsed route healthy → continue
        |
        +── parsed=0 / raw>0
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
independent raw minimum-time boundary verification
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

The Worker/Supervisor agents are **exception reviewers only**. They cannot override Python identity validation, chronology verification, evidence validation gates, or release benchmark answers.

---

## Evidence and Validation

ALR separates AI reasoning from deterministic validation.

### AI Reasoning

Used for:

- Investigation planning
- Interpretation
- Strategy selection
- Anomaly review

### Deterministic Validation

Used for:

- Identity checks
- Chronology ordering
- Evidence extraction
- Source validation
- Final answer formatting

This separation prevents unsupported conclusions from being promoted as facts.

---

## Codename Resolution

ALR legacy compatibility mode does not contain a release/codename answer table.

The resolution process is:

```text
AMI + region
      ↓
External evidence
      ↓
Ubuntu version
      ↓
Official Ubuntu release page
      ↓
Two-word codename evidence
```

Once an Ubuntu version is established, ALR constructs official Ubuntu release URLs directly.

Search-engine discovery is only a fallback mechanism and is restricted to official Ubuntu hosts.

---

## Agentic Web Enrichment

### ALR v0.3.1

ALR v0.3.1 added agentic web enrichment capability.

This enables ALR to:

- Retrieve external evidence sources
- Extract structured information
- Validate source relationships
- Enrich investigation context

External enrichment is used for evidence collection and validation, not as an uncontrolled answer source.

---

## Fresh Benchmark Isolation

Each Option 1 execution creates a fresh investigation boundary.

Before the benchmark begins:

```text
previous mutable ALR state
        ↓
ALR_benchmark_archive/<timestamp>
```

Previous evidence is preserved for inspection but cannot silently influence a fresh benchmark run.

---

## Benchmark Contamination Boundary

Benchmark question files contain:

- Questions
- Metadata
- Evaluation structure

They do **not** contain:

- Answers
- Release/codename tables
- Hidden investigation shortcuts

Automatic scoring occurs only after investigation completion through an isolated, normalised-answer SHA-256 oracle.

The oracle is never passed to models or investigation tools.

> **Note:** Some benchmark questions may be skipped or refused when an LLM flags them under provider-specific safety policies. Such refusals are external platform behaviour and do not necessarily indicate benchmark contamination or an ALR workflow failure.

---

## Diagnostics

Useful files generated after Option 1 include:

- `ALR_sequence_summary.json`
- `ALR_q6_stage_trace.json`
- `ALR_q6_resolution.jsonl`
- `ALR_eval_runs.jsonl`
- `ALR_timeline.jsonl`
- `ALR_relationships.jsonl`
- `ALR_benchmark_run.json`

---

## Validation

The development tree includes automated validation covering:

- Chronology handling
- Raw failover
- Independent verification
- Anomaly review
- Randomised literal independence
- Benchmark isolation
- Relationship provenance
- Direct official codename resolution
- Contamination boundaries
- Supervisor behaviour

### Current Validation Status

**183 automated tests passing**

---

## Requirements

ALR requires:

- Windows or Linux
- Python 3.x
- Git
- Ollama for local LLM execution
- Splunk/BOTSv3 environment for benchmark workflows

---

## Hardware Requirements

ALR supports local LLM execution through Ollama.

| Model size | Approximate requirement |
|---|---|
| 3B–8B | 4–12 GB VRAM, depending on quantisation |
| 20B class | Higher VRAM requirements or CPU offload |

More VRAM enables:

- Larger context windows
- Faster inference
- Reduced CPU fallback

Hardware-specific model selection should be configured locally.

---

## Project Structure

```text
ALR/
|
├── commander_agent/
│   ├── core/
│   ├── reasoning/
│   ├── skills/
│   └── state/
|
├── benchmarks/
├── config/
├── scripts/
├── tests/
|
├── alr.py
├── setup-ALR.cmd
└── run-ALR.cmd
```

---

## Setup

Extract ALR into a new directory and run:

```text
setup-ALR.cmd
run-ALR.cmd
```

Press:

```text
1
```

to execute:

```text
Q1 → Q2 → Q3 → Q6
```

using the evidence-validated sequence state.

Setup displays individual pytest progress and test names during execution.

---

## AI Platform Safety and Benchmark Behaviour

ALR is designed for legitimate cybersecurity investigation, incident-response training, evidence validation, and defensive security research.

Some benchmark questions, investigation tasks, data sources, or analysis workflows may be interpreted by certain Large Language Model platforms as sensitive security activity.

As a result, behaviour may differ between providers, models, account types, and safety-policy versions.

Possible outcomes include:

- Refusal to answer specific benchmark questions
- Questions being skipped after they are flagged by the LLM
- Partial investigation assistance
- Incomplete reasoning chains
- Restricted access to certain investigative workflows
- False-positive classification of legitimate security research

Users should not attempt to bypass, evade, jailbreak, or defeat platform safety controls.

Repeatedly asking a model to perform tasks that its provider has classified as restricted, attempting to force it to complete flagged tasks, or repeatedly rephrasing blocked requests may result in provider-defined moderation or enforcement responses.

Depending on the provider, these responses may include:

- Safety or account warnings
- Additional moderation checks
- Temporary usage restrictions
- Rate limiting
- Automated account reviews
- Other actions defined by the provider's terms of service

ALR does not include mechanisms for bypassing model safety systems and is not intended to circumvent platform policies.

If a model declines or skips a benchmark task, users should respect the provider's policies and consider:

- Using a compliant investigation workflow
- Applying approved manual analysis techniques
- Selecting an alternative model configuration where permitted
- Executing the investigation using deterministic tooling only

The inclusion of a benchmark question within ALR does not imply that every AI platform will permit direct execution of every investigative step.

Safety and policy behaviour is controlled by each AI provider and may change over time. Users are responsible for ensuring that their use of ALR complies with applicable laws, organisational policies, and platform terms of service.

---

## Security

Do not commit:

- API keys
- Credentials
- Private configuration
- Runtime state
- Generated logs

Use:

```text
config/credentials.example.json
```

as the configuration template.

See:

```text
SECURITY.md
```

for vulnerability-reporting guidance.

---

## Acknowledgements

Thanks to the contributors of the [BSides Canberra 2026 Copilot project](https://github.com/graphistry/bsides-canberra26-copilot) for providing inspiration and practical examples that helped shape this project.

---

## Licence

MIT License