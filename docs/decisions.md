# Decisions and architecture evaluation

Why Astronomo is built the way it is, and where its gaps are. The plan is in the [roadmap](../ROADMAP.md); this page is the reasoning behind it.

Moved out of the roadmap on 2026-11-20 with the text unchanged. **The phase numbers and phase names inside this page are the old ones** (Phases 1 to 6 before the reorganization). In the current roadmap: old Phases 3, 4 and 5 (bounded LLM decisions, close the loop, measure the models) are now [Phase 7](../ROADMAP.md#phase-7-autonomy); old Phase 2 (unattended and trustworthy) is now [Phase 3](../ROADMAP.md#phase-3-first-accepted-file), [Phase 4](../ROADMAP.md#phase-4-claims-you-can-defend) and [Phase 5](../ROADMAP.md#phase-5-unattended-and-automatic); old Phase 1 (first light) is now [Phases 1 and 2](../ROADMAP.md#phase-1-public-release); old Phase 6 (reach) is now [Phase 8](../ROADMAP.md#phase-8-reach).

## Decisions

### Participation over discovery: network targets and upload files (2026-10)

Question raised: should the harness keep doing generic discovery runs, or should it work for the collaborative networks that actually use amateur data?

**Decision:** participate. Targets come from the networks' priority lists, ranked in code and tagged with their source, task and reason. Output defaults to standardized upload files (AAVSO, ADES) that a person reviews and sends; an automated Markdown mission log is written every time, and a screened anomaly (`ASTRONOMO_ANOMALY_MIN_SIGMA`, default 5) is flagged in it for a person to review.

Why: a 50 mm telescope cannot out-discover a survey, but ephemeris refinement, TTV monitoring, NEOCP astrometry and transient photometry are tasks where the networks say amateur data helps. A write-up per routine night would mostly describe pipeline demonstrations (see [Real-data results](status.md#what-is-not-real-yet)). A file in the network's format is the product that is actually useful, and a person can review it quickly.

Rules that came with it:

- The **anomaly threshold is applied in core**, not by plugins, and an identity that was not confirmed (for example a transit timing measured on a star not matched to the host) scores 0. A flagged anomaly is a lead needing independent confirmation, never a discovery.
- The **LLM stays advisory** and writes only the observing rationale on the dashboard; the mission log contains no model text, and no ranking or screening decision comes from a model.
- **Astronomo never sends.** `SIMULATED_`, `REPLAY_` and `DRAFT_` file names, a `manifest.json` with `sent: false`, and blockers (a missing observer or station code, an unconfirmed identity) keep a file from looking ready when it is not.
- Network failure degrades visibly: a cached copy is used (and flagged stale), then a labelled built-in list (`manual`), never silently.

### Orchestration: deterministic graph, not an LLM agent (current state, 2026-10)

Question raised: which LLM does Astronomo use, and is there an orchestrator that calls tools?

- **Model:** none is pinned. `LLM_PROVIDER=none` is the default. Local (Ollama; `llama3.1` fallback, `qwen2.5-coder:7b` in the docs), Anthropic and OpenAI (model id must be set explicitly) and any OpenAI-compatible server are supported.
- **Orchestrator:** a LangGraph `StateGraph` in `astronomo/core/graph.py`. It is deterministic. Routing is plain Python conditional edges, and missions are checkpointed in SQLite so they can resume.
- **Tool calling:** the graph calls tools (telescope, weather, catalogs, plugin reduction). The LLM does not. There is no function calling, tool schema or agent loop.
- **LLM role:** one advisory prose call per mission (the `generate_hypothesis` rationale), fed only a digest built with `core/digest.py`. Nothing it writes affects weather gating, hardware, detection or classification.

**Decision:** keep the deterministic graph as the orchestrator and safety spine. Do not replace it with a free-roaming tool-calling agent. Any added autonomy comes as bounded decision nodes inside the graph (propose, then validate in code). Safety interlocks (weather, sun altitude, hardware limits, token budgets) stay in code and are never delegated to a model.

Why: the hardware is real and optics can be damaged, missions run for hours or days and must resume after a crash, and results must be reproducible and auditable. A deterministic spine gives all three. Model choice then becomes a per-node setting, which is what the benchmark phase needs.

This does not contradict the north-star loop. The goal is still the full cycle (propose, run, analyze, test, submit or rework); the decision is about *how* it is reached: one bounded, validated decision node at a time (Phases 3 and 4), each with a deterministic fallback, rather than a model with open-ended tool access. The pipeline can run a mission without a person at each step. The model joins those decisions one bounded node at a time, and it stays off unless someone turns it on.

Parked questions (answered in Phase 3, not before):

- Should different nodes be able to use different models (for example a small local model for `evaluate_data` and a larger one for `propose_mission`)? Per-node `LLM_*` overrides would be needed.
- When a decision node's output is invalid or the model is down, the fallback is the current deterministic behaviour (priority sort, always accept). This is consistent with the existing `llm_strict` policy.

## Architecture evaluation (astronomo)

Evaluated from `astronomo/core/graph.py`, the README and the config. Short version: this is a well-built **workflow with an LLM garnish**, not an agent. That is a good fit for the hardware, but it limits what LLM benchmarking can measure. The phase for each gap is in brackets.

### Strengths

- **Safety by construction.** Weather, darkness and hardware gating are deterministic and fail safe (missing weather data means do not observe). The model cannot influence them.
- **Durable and resumable.** LangGraph SQLite checkpoints plus the lease/clock design let a multi-day mission survive restarts.
- **Clean plugin boundary.** `tools/` and `core/` know no science case. A test enforces this, so adding an experiment does not touch the orchestrator.
- **Strong data firewall.** `core/digest.py` rejects, not truncates, raw arrays, FITS dumps and oversized payloads, and only `graph.py` and `main.py` may import the LLM client.
- **Cost and failure control.** Per-call and per-mission token and call budgets, a response cache, a ledger of every prompt, and graceful fallback to deterministic text (or fail loudly with `llm_strict`).
- **Observable.** Every node start, end and error is mirrored to the DB and shown in the dashboard.
- **Testable.** Full missions run against fake LLM servers and a mock sky, so no hardware, key or cost is needed.

### Weaknesses and gaps

1. **The model has no agency.** Target selection and ordering come from the plugin (`generate_targets`) and a priority sort. The LLM's "rationale" is generated *after* the targets are fixed, so it is post-hoc explanation and cannot change the plan. [Phase 3]
2. **No closed loop.** The pipeline is linear: observe, reduce, cross-reference, next target. Nothing reacts to results, for example re-exposing a low-SNR field, following up a candidate, or dropping a target that keeps failing quality checks. [Phase 3 for re-observe/skip, Phase 4 for the full loop]
3. **No tool abstraction an LLM could use.** The de facto tools are plugin methods and adapter classes with no registry, schemas or permission model. Agent-style tool calling would need that layer first. Not planned: the decision says to use bounded decision nodes instead.
4. **Scheduling waits for a person.** `python main.py --schedule` resumes a weather wait and lists due plans. It starts one only with `--confirm`. Self-start stays off until the interlocks and parking have been confirmed on this mount. The Plans page lists every saved plan. A person rehearses or starts one from there, or with `--from-plan`. Weather that outlasts `max_sleep_cycles` is deferred with a wake time, not aborted. It does not move to another target while the site is cloudy, because the block is the whole sky. [Phase 2, the software; the unattended night itself is still open]
5. **Retries exist and are unproven on a real fault.** A transient network or link error is retried (`ASTRONOMO_NODE_RETRIES`). A device error is not. The policy has met simulated faults only. [Phase 1]
6. **Thin LLM surface for benchmarking.** Two prose tasks of up to 160 and 300 output tokens do not discriminate much between models, and quality is judged only by a number-grounding check. [Phases 3 and 5]
7. **Single provider config.** One global `LLM_`* setting means every node uses the same model, so cross-task comparison is not expressible yet. [Phase 3]
8. **Mixed responsibilities in `generate_hypothesis`.** It validates and persists targets and also calls the LLM. Splitting the LLM part into its own node would make it replaceable, skippable and benchmarkable on its own. [Phase 7.1](../ROADMAP.md#phase-7-autonomy)
9. **Mission state grows with the mission.** `observations` accumulates inside the checkpointed state. That is fine for a few targets but worth watching for long multi-day surveys. [Phase 2]

