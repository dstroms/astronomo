# Using a language model

A language model is optional and off by default. This page covers setup (local or cloud), verification,
what the model does, and the rules that keep it from controlling anything. Back to the
[README](../README.md).

## Choose a model: local, cloud, or none

A language model is **optional and advisory**: it writes two short pieces of prose (an observing rationale
and one Discussion paragraph) and controls nothing. Pick one:

| | Cost | You need | Set in `.env` |
|---|---|---|---|
| **None** (default) | free, zero tokens | nothing | `LLM_PROVIDER=none` |
| **Local, Ollama** | free (your hardware) | [Ollama](https://ollama.com) + a pulled model | `LLM_PROVIDER=ollama` |
| **Cloud, Anthropic / OpenAI** | pay per token | an API key + a model id | `LLM_PROVIDER=anthropic` or `openai` |

Switching is a configuration change only; no code changes. Start with **local** to verify the integration at
zero cost, then move to a cloud model if you want better prose.

### A. Local setup: Ollama (zero cost)

**1. Install Ollama** from [ollama.com](https://ollama.com/download) (macOS and Windows installers; on Linux
`curl -fsSL https://ollama.com/install.sh | sh`, or `brew install ollama` on macOS; check ollama.com for current
instructions). The desktop app normally starts the server for you; with a CLI-only install, run it yourself:

```bash
ollama serve            # leave running in its own terminal (listens on http://localhost:11434)
```

**2. Pull a model** (a one-off download of several GB; a 7B model wants roughly 8 GB of free RAM or VRAM):

```bash
ollama pull qwen2.5-coder:7b     # recommended starting point
ollama list                      # confirm it is installed
```

`qwen2.5-coder` is tuned for code: it is fine for checking that Astronomo works and for short, factual
summaries. `ollama pull llama3.1` is a general-purpose alternative that tends to write more natural prose.
Any model Ollama can serve will work; pin the exact tag you pulled (`qwen2.5-coder:7b`, not just `qwen2.5-coder`).

**3. Configure** `.env` (created in Quick start step 3). Set exactly these lines:

```bash
LLM_PROVIDER=ollama
LLM_MODEL_NAME=qwen2.5-coder:7b
LLM_BASE_URL=http://localhost:11434
```

(`.env.example` has this block ready to uncomment. If a key appears twice in `.env`, the last one wins.)

**4. Verify, then run:**

```bash
python main.py --llm-check                  # connectivity + model installed + one tiny request
python main.py --simulate --fast            # full mock mission; the model writes the two paragraphs
python main.py --simulate --fast --ollama   # same, in VERIFICATION mode (see below)
```

The run prints your model and its token usage (illustrative numbers; a real model writes longer replies):

```
Astronomo | plugin=asteroid_finder | ... | llm=ollama:qwen2.5-coder:7b @ http://localhost:11434 | ...
Mission m_... finished: COMPLETE
  llm: 1 call(s), 319 prompt + 8 completion tokens, 0 cache hit(s), 0 failed, largest prompt 442 chars
```

**Verification mode.** `--ollama [MODEL]` sets the provider to Ollama, makes it **strict** (any model problem
fails the mission loudly instead of silently falling back to plain text) and turns the **cache off** (so the model
is really called every time). Use it when you are checking an integration, not for normal runs. The same check as
a test: `pytest -m ollama -v -rs`.

**Troubleshooting**

| You see | Meaning / fix |
|---|---|
| `cannot reach http://localhost:11434 ... Is Ollama running?` | Start it: `ollama serve`. |
| `model 'x' is not installed ... Run: ollama pull x` | `ollama pull x`; make sure `LLM_MODEL_NAME` matches `ollama list` exactly. |
| `timed out after 180s` | The first request loads the model into memory and can be slow. Raise `LLM_TIMEOUT_S`, or use a smaller model. |
| `no usable text ... <think>` | A reasoning model spent its whole output cap thinking. Raise `LLM_MAX_OUTPUT_TOKENS` or use a non-reasoning model. |
| `prompt used N tokens, filling the context window` | Raise `LLM_NUM_CTX` (Ollama truncates silently otherwise; Astronomo detects it). |
| Mission is `COMPLETE` but prints `! llm(...)` lines | Non-strict mode: the model failed and the report fell back to deterministic text. Add `--llm-strict` (or `LLM_STRICT=true`) to make it fail instead. |
| Ollama runs on another machine | Set `LLM_BASE_URL=http://<host>:11434`; the server must listen on a reachable address (see Ollama's `OLLAMA_HOST` setting). |

### B. Cloud API setup (frontier models)

**1. Get an API key** from your provider's console and enable billing (these APIs are pay-per-token):
Anthropic: [console.anthropic.com](https://console.anthropic.com), OpenAI: [platform.openai.com](https://platform.openai.com).

**2. Choose a model id** from the provider's documentation and set it explicitly. Astronomo deliberately has
no built-in default for cloud models: ids change over time, and a stale default would fail or bill unexpectedly.

**3. Configure `.env`** (never put keys in code or commit them; `.env` is git-ignored):

```bash
# Anthropic
LLM_PROVIDER=anthropic
LLM_MODEL_NAME=<model-id-from-anthropic-docs>
ANTHROPIC_API_KEY=<your-key>

# ...or OpenAI
LLM_PROVIDER=openai
LLM_MODEL_NAME=<model-id-from-openai-docs>
OPENAI_API_KEY=<your-key>
```

Prefer the shell? `export ANTHROPIC_API_KEY=...` works too and takes precedence over `.env`.
The key can also be given as `LLM_API_KEY` (or `ASTRONOMO_LLM_API_KEY`), which wins over the provider-specific name.

**4. Verify, then run:**

```bash
python main.py --llm-check          # one tiny request (at most 64 output tokens: negligible cost)
python main.py --simulate --fast    # full mock mission using the cloud model
```

**Cost control** is enforced by Astronomo, not left to you: a mission makes **one call**, the prompt is a
concise summary (typically a few hundred tokens), output is capped (160 tokens), and there is a hard
per-mission ceiling (`LLM_MAX_TOKENS_PER_MISSION=20000`, `LLM_MAX_CALLS_PER_MISSION=8`) checked *before* each
request. Expect well under 1k tokens per mission. Identical requests are served from a local cache.
Every call, its token counts and the exact prompt are in the dashboard's Activity section ("Language model").

**Security.** API keys are read from the environment, are never written to the database or logs, and are
excluded from `repr()` of the settings. Keep `.env` out of version control and out of shared screenshots/issues.

**Any OpenAI-compatible server** (vLLM, LM Studio, llama.cpp, a hosted gateway): use `LLM_PROVIDER=openai`
with `LLM_BASE_URL=http://localhost:8000/v1` and `LLM_MODEL_NAME=<served-name>`; no key is needed for local servers.

> **Not yet verified against live services.** Ollama and the cloud APIs were not available where this was
> built. The client is tested against fake servers that implement each provider's documented API, so treat your
> first real `--llm-check` as the actual verification, and start with the local model.

## Language models (optional)

By default **no model is used and zero tokens are spent**: every sentence in the mission log, and the upload files is deterministic. Set a provider and one node asks a model for *advisory prose*:

| Node | Ask | Cap |
|---|---|---|
| `generate_hypothesis` | ≤ 3-sentence rationale for the target list/order | 160 output tokens |

That is **one call per mission**, however many targets. The rationale is shown on the dashboard; the mission log and the upload
files never contain model text, and the log's reproducibility table records which model was used. A plugin's
`summarize_for_llm` digest hook is kept for later uses of a model (see the roadmap), but nothing sends it today.
**The model never gates weather, hardware, detections or classifications**; those are deterministic
and nothing it writes feeds back into them.

### Setup and verification

Setup (Ollama, Anthropic, OpenAI, OpenAI-compatible), verification mode and troubleshooting are in
[Choose a model](#choose-a-model-local-cloud-or-none). Start with a local model to verify the integration for free.

### The data-reduction rule

Everything heavy is reduced in plain Python (astropy, photutils, scipy). A model sees only a short, rounded
JSON *digest*, and this is enforced in code, not by convention:

- `plugin.summarize_for_llm(findings, crossref, target)` is the **only** view of an observation a model can get.
  The default is default-deny (scalars and container *sizes* only). The asteroid plugin returns a per-track digest
  built from `reduction.summarize()` (id, RA/Dec, rate, SNR, detections/frames, quality, Horizons verdict, Gaia
  calibration), roughly 500 characters per field in the simulated missions I measured.
- Every prompt is rendered through `core/digest.py`, which **rejects, rather than truncates**: numpy arrays,
  astropy tables/HDUs, long numeric lists (what `ndarray.tolist()` produces), FITS-header dumps, base64-like blobs,
  deep nesting, and anything over the character budget. A plugin that forgets to reduce something fails loudly
  (blocked, recorded in the ledger, nothing sent) instead of burning tokens.
- `tests/test_data_rules.py` audits that `reduction.py`'s summary passes the firewall while the raw findings
  would be refused, and that nothing in `tools/`, `plugins/`, `writer/`, `db/` or `dashboard/` can import the LLM
  client (only `core/graph.py` and `main.py` do).
- `tests/test_llm_graph.py` runs full missions against a fake server and asserts on the bytes that actually went
  over the wire: no `reference_stars`, WCS, per-frame points or FITS paths, a handful of numbers, small prompts.

### Token-waste controls

Hard caps on prompt size and output tokens; per-mission call and token budget (checked *before* sending, using a
worst-case estimate for the call about to be made, and stored in the DB so it survives a restart); an on-disk
response cache so a node re-run after a crash costs nothing; retries only for transient errors (timeouts,
429/5xx), never for client errors; reasoning-model `<think>` blocks are stripped; and a **ledger** (`llm_calls`
table, dashboard Activity section, CLI summary) records every call with token counts and the exact prompt and
response, including blocked and failed attempts. In a simulated 2-target mission the whole run was 1 call.

### Caveats

- **Not yet verified against a real Ollama or any cloud API.** Ollama wasn't available where this was built. The
  client is tested against a fake server implementing each provider's *documented* wire format (shape, auth,
  errors, retries, usage fields), and `tests/test_ollama_live.py` is the opt-in test that confirms it on your machine.
  Treat the first real runs as the actual verification.
- **Generated prose can be wrong.** Small local models do invent things, so the rationale is advisory and
  nothing in a mission, a file or the mission log depends on it.
- Ollama's default context window is small and it truncates **silently**; the client sets `num_ctx`
  (`ASTRONOMO_LLM_NUM_CTX`) and errors if the reported prompt size fills it. A code-tuned model such as
  `qwen2.5-coder` is fine for checking state transitions but is not a good science writer.
- Reasoning models (`qwen3`, `deepseek-r1`, ...) can spend the whole output cap on `<think>` text; that is reported
  as an error ("no usable text") rather than an empty paragraph. Raise `ASTRONOMO_LLM_MAX_OUTPUT_TOKENS` for them.
- Token counts are the server's; if a server omits them they are estimated (pessimistically) and marked. I believe
  Ollama can omit prompt counts when its own prompt cache is hit, but that is from memory, not something I observed.
- The response cache is keyed on the exact request, so it helps a re-run of the *same* node with the *same* data
  (e.g. `generate_hypothesis` after a crash). It will not help if the inputs differ, as in live mode where ephemeris
  values change between runs.
