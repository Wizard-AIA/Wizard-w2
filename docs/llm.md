# LLM, Providers & Embeddings

> Deep reference for the Wizard w2 model layer. The concise rules live in
> [`backend/CLAUDE.md`](../backend/CLAUDE.md); this file explains the full
> design.

---

## LLM Provider — `core/llm/`

`llm_provider` builds and caches clients keyed by `(provider, endpoint, model,
temperature, max_tokens, num_ctx)`, so per-session model selection is cheap.
Every entry point has a streaming twin (`astream`, `stream_to`), and every one
takes `max_tokens`.

---

## Output Budgets

`settings.output_budget("decision"|"plan"|"code"|"answer"|"review")`, clamped
to `MAX_TOKENS`. `MAX_TOKENS` used to be the only number, so every call was
allowed 4096 tokens regardless of purpose. That is free when a model stops on
its own and ruinous when it does not: a decision worth sixty tokens could run
to four thousand, which on a CPU-bound 1.5B model is four minutes.

`max_tokens` is part of the client cache key. `num_ctx` is *not* varied per
call.

The Ollama client also sends `keep_alive` and a request timeout via
`client_kwargs` — `ChatOllama` has no `timeout` field.

---

## Memory Fitting — `llm/resources.py`

The manager and worker alternate several times per question, so whether they
can be **resident at once** decides whether that alternation is free or
ruinous.

- `estimate_footprint` is **calibrated against a measurement** — `qwen2.5:3b`
  is 1.93 GB on disk and 2.91 GB resident at 8192 context, giving ~40 MB of
  KV cache and buffers per billion parameters per 1024 tokens. Deliberately
  biased high.
- `plan_resident_set` compares the pair against `MODEL_MEMORY_FRACTION` (0.60)
  of system RAM. Fits → both keep `LLM_KEEP_ALIVE` (30m). Does not fit →
  `LLM_KEEP_ALIVE_SWAP` (30s), short enough to expire while the other model
  works.
- Only `LOCAL_PROVIDERS` are planned. Same model in both roles collapses to
  one footprint and never swaps.
- `ModelSpec.keep_alive` is part of the client cache key.

---

## Reasoning Models — `llm/reasoning.py`

A model's private thinking is not its answer. `split_reasoning` /
`strip_reasoning` remove `<think>`, `<thinking>`, `<thought>`, `<reasoning>`
and `<reflection>` blocks; `ReasoningStream` does the same **incrementally**,
holding back only a trailing partial tag.

The orchestrator knew about `<thought>` — the tag its own planning prompt asks
for — and nothing else. With `deepseek-r1:1.5b` the raw chain of thought
became `state.plan`, and **the plan is embedded in every later decision prompt
and in the answer prompt** — prepending a thousand tokens of deliberation to
five later prompts on the machine least able to re-read them. It also broke
action parsing and streamed the deliberation to the user as the answer.

`_extract_code` strips reasoning **first** — a model drafts code inside
`<think>`, discards it, and writes the real thing afterwards. Searching the
raw response runs the draft the model already rejected.

An unclosed block yields empty visible text; callers treat that as "nothing
usable".

---

## Provider Per-Request

**The provider is per-request, not process-wide.** `settings.API_PROVIDER` is
only the default. `ModelPreferences` stores a provider per *role*, so one run
can plan on Ollama and generate code on LM Studio. `ModelSpec` carries the
resolved `base_url`, and that URL is part of the cache key. **Never read a
provider URL directly from `settings`**; go through
`settings.provider_root_url` / `provider_openai_base_url` / `provider_api_key`,
keyed by the provider actually in play.

---

## What a Provider Is — `providers.py`

One row per backend: id, label, `kind` (local/cloud), `api_style`
(ollama/openai/anthropic), default URL, which settings fields hold its URL and
key, and whether it needs one. Adding Groq or a self-hosted vLLM is a row, not
a code change.

This replaced an `if name == "ollama" … elif` chain repeated in four places.

It sits **beside `config.py`, not under `core/llm/`** — `Settings` is built at
import time and reads this table for its defaults, while `core.llm.__init__`
imports `settings` back.

`is_cloud()` treats an **unknown** provider as cloud. That feeds the data-mode
check.

`openai_suffix` is only non-empty for LM Studio; a hosted endpoint is
configured with its version segment already in it.

---

## Model Registry — `llm/registry.py`

`model_registry` enumerates what is really installed, per provider and cached:

| Provider | Endpoint | Notes |
|---|---|---|
| Ollama | `/api/tags` | |
| LM Studio | `/api/v0/models` (native), fallback `/v1/models` | |
| Anthropic | `/v1/models` with `x-api-key` + `anthropic-version` | asked only when a key exists |
| OpenAI / gateways | `/v1/models` with bearer token | |

Empty results are cached too, for a shorter TTL. `available_providers()` must
stay network-free.

---

## Usage & Cost — `llm/usage.py`

`extract_usage` reads `usage_metadata`, then `response_metadata`
(`token_usage`, or Ollama's `prompt_eval_count`/`eval_count`), then falls back
to a `len/4` estimate flagged `exact=False`. Three shapes on purpose.

`usage_ledger` is keyed by session id rather than held on the `Session`, so
`core/llm` does not import `core.session`. A streamed call is booked **once**.

**An unpriced model reports tokens and `cost_usd: None`**, never a guess, and
is named in `unpriced_models` so the readout can say the total is a floor.
Under `local-only` the API returns `local_only: true` and no cost at all.

---

## Installing Models — `llm/downloader.py`

`POST /api/models/download` — getting a model was the one setup step that sent
you out of the tool.

| Provider | Method | Notes |
|---|---|---|
| Ollama | `POST /api/pull` (streams NDJSON), `DELETE /api/delete` | real API |
| LM Studio | `lms` CLI (spawned) | no download API; reports % not bytes; no delete verb |
| Gateways | — | hosts their models; says so |

The model name reaches an argv, so it is **validated, not escaped**
(`is_valid_model_name`). A URL is matched against the Hugging Face pattern
*first* — testing the general pattern first lets `https://evil.example.com`
match. Requiring an alphanumeric first character blocks flag injection.

Downloads run on a thread and are **polled, not streamed**. `DownloadState.finish()`
writes `finished_at` *before* `status`.

`lms_executable()` checks PATH then `~/.lmstudio/bin/` — the installer does
not add itself to PATH on Windows. Inside a container `capability()` says that
rather than letting the button fail.

---

## Embeddings — `embeddings.py`

Resolution order: **provider endpoint → local sentence-transformers (only if
installed) → hashing encoder**. Nothing here raises; degrading beats failing.

`sentence-transformers` is no longer a dependency. It requires torch +
~2.8 GB of CUDA wheels. See `requirements-optional.txt` for how to put it back
from the CPU index.

### API shapes

- Ollama: `POST /api/embed` → `{"embeddings": [[...]]}`.
- Everything else: `POST /v1/embeddings` → `{"data": [{"index", "embedding"}]}`,
  **sorted by index** — order is not promised.

### Discovery and startup

- Model discovered through `model_registry`'s classification, then **probed
  once** before adoption.
- **Resolved at startup on a background thread** (`embedding_service.warm()`);
  `encode` answers from the hashing fallback while in flight.
  `EMBEDDING_COLD_TIMEOUT` (180s) applies to the adoption probe only;
  `EMBEDDING_TIMEOUT` (20s) governs steady state.
- A missing or unreachable encoder is remembered for `REMOTE_RETRY_SECONDS`,
  **doubling per consecutive failure** up to `REMOTE_RETRY_MAX_SECONDS`.
  The failure is stamped *after* the attempt.
- An encoder that loads but cannot encode is dropped. `EMBEDDING_ALLOW_DOWNLOAD`
  is off.
- `rank()` re-encodes a stored vector whose **width** differs from the query's.
