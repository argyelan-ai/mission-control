# Choosing AI providers

Mission Control does two kinds of thinking, and they are easy to confuse.

**Your agents** think with whatever their runtime gives them — Claude Code,
an OpenAI-compatible endpoint, a local vLLM box. That is configured per agent
under *Runtimes* and is not what this page is about.

**Mission Control itself** also thinks, quietly, in three places:

| Function | What it does for you | Why you'd change it |
|---|---|---|
| **Embeddings** | Turns every memory entry and vault note into a vector, so memory search finds things by *meaning* instead of by keyword | You have no GPU box, or you want a different embedding model |
| **Insights** | Writes one report a day about patterns, outliers and anomalies across the fleet | You have no GPU box, or you simply don't want the report |
| **HuggingFace** | Powers the model browser: search, file listing, download onto your box | You need a **gated** repo (Llama, Gemma, many others) |

Each of the three is optional and each degrades quietly. MC does not stop
working without them — memory search falls back to keyword matching, the
daily report is skipped, and the model browser sees public repos only.

Everything below lives under **Settings → Connections → AI providers**, and
every change takes effect immediately. No restart.

## How a setting is decided (three layers)

This is the same mechanism the channels page uses, so it is worth learning
once:

1. **`.env`** sets the default. A fresh install works from `.env` alone.
2. **The settings page** overrides `.env` and stores the decision in the
   database. Pinned values are marked *overridden* in the UI, so you can
   always see which values are yours and which came from `.env`.
3. **Keys** (HuggingFace, Ollama Cloud) never live in either — they go into
   the encrypted vault, from the same page.

Leave an override field empty and it inherits the selected provider's own
default. You only fill in what should differ.

## The providers

| Provider | Function | Means | Needs a key |
|---|---|---|---|
| `spark` (shown as *Self-hosted*) | Embeddings + Insights | **Your own machine** — any OpenAI-compatible endpoint (vLLM, LM Studio, llama.cpp's `llama-server`). The default. | Optional — only if your endpoint sits behind auth |
| `cloud` | Embeddings | **Any hosted OpenAI-compatible** `/v1/embeddings` (Together, DeepInfra, Fireworks, …) | Yes, that host's API key |
| `ollama_cloud` | Insights | **ollama.com**, the hosted Ollama service | Yes, an Ollama Cloud key |
| `off` | Insights | No report is generated | — |

> **Ollama Cloud means ollama.com, not a local Ollama.** Running Ollama
> locally next to MC is deliberately not an option here. On Apple Silicon it
> competes with Docker for the same unified memory and can take the whole
> machine down. If you want local inference, run it on a separate box and
> point the self-hosted provider at it.
>
> Ollama Cloud is an **insights** arm only: ollama.com hosts chat models but
> no embedding models, and it has no OpenAI-compatible `/v1/embeddings` path.
> For hosted embeddings use the `cloud` provider with a host that actually
> serves your embedding model.

## 1. Embeddings

Two arms, and **each arm keeps its own endpoint and model** — that is what
makes the provider select a real one-click switch. Flipping between
self-hosted and cloud never drags a URL from the other side along.

**Self-hosted** (the default):

```
AI_EMBEDDINGS_PROVIDER=spark        # spark = self-hosted | cloud
AI_EMBEDDINGS_URL=                  # empty = SPARK_EMBEDDING_URL from .env
AI_EMBEDDINGS_MODEL=                # empty = SPARK_EMBEDDING_MODEL from .env
```

Point it at whatever OpenAI-compatible server you run — LM Studio, vLLM, or a
tiny `llama-server` (from llama.cpp) with an embedding GGUF. If your endpoint
requires a bearer token, store the optional *Self-hosted key* on the settings
page.

A **fresh install ships with no endpoint configured**. That is a deliberate
state, not an oversight: MC saves memories without a vector, attempts no
network call, and the settings page tells you what to fill in. (Earlier
versions shipped a placeholder IP as the default — which meant every memory
insert hammered a dead address. Don't recreate that: point the URL at a real
server or leave it empty.)

**Cloud**:

```
AI_EMBEDDINGS_CLOUD_URL=            # e.g. https://api.together.xyz/v1/embeddings
AI_EMBEDDINGS_CLOUD_MODEL=          # e.g. nomic-ai/nomic-embed-text-v1.5
```

Pick a host that serves the **same embedding model** as your self-hosted side
and store its API key as the *Cloud embeddings key*. Same model, same
vectors — you can switch back and forth freely.

**One thing to watch: vector size.** MC's vector store is built for 768
dimensions. A model that returns a different size does not simply perform
worse — its vectors cannot be compared to the ones already stored. The
**Test embeddings** button tells you the size you actually get back, and the
UI flags a mismatch. If you do switch to a differently-sized model, the
existing vectors have to be rebuilt.

## 2. Insights

```
AI_INSIGHTS_PROVIDER=spark          # spark | ollama_cloud | off
AI_INSIGHTS_MODEL=                  # empty = whatever your box is serving
```

With `spark` and no model pinned, MC asks your box which model it is
currently serving and uses that — so a recipe swap needs no follow-up edit
here.

Setting this to `off` stops the daily report and changes nothing else. The
rule-based half of the intelligence service (durations, success rates,
failure patterns, anomalies) needs no LLM at all and keeps running.

## 3. HuggingFace

Without a token MC talks to HuggingFace anonymously and sees public repos
only — which is enough for most models. A token buys you exactly one thing:
**gated repos**, the ones where you accept a licence on the website first.

Create one at
[huggingface.co/settings/tokens](https://huggingface.co/settings/tokens);
a **read** token is enough. Paste it into the HuggingFace field on the
settings page and press **Test HuggingFace** — it should report the account
name it belongs to.

The token is then used in all three places the model browser touches
HuggingFace: search, file listing, and the download running on your GPU box.
Getting one of the three wrong is the classic trap — finding a model you then
cannot download.

## Keys

All keys are stored encrypted in MC's vault, never in `.env`, and are only
ever read by the functions above:

| Key | Used by |
|---|---|
| `hf_token` | Model browser (search, files, download) |
| `embeddings_api_key` | Self-hosted embeddings — optional, only if your endpoint requires auth |
| `embeddings_cloud_api_key` | The `cloud` embeddings arm |
| `ollama_api_key` | Insights, when set to `ollama_cloud` |

> **These keys never reach your agents.** MC deliberately does not hand a
> stored key to an agent runtime as a fallback (ADR-056): a runtime that gets
> a key it did not ask for is how a keyless local model ends up billing a
> paid cloud account. Agent credentials are bound per agent or per runtime,
> and nowhere else.

If you select `ollama_cloud` without storing a key, the page says so plainly —
ollama.com would reject those calls with a 401.

## Full `.env` block

```
# Which provider serves MC's own AI functions.
AI_EMBEDDINGS_PROVIDER=spark        # spark = self-hosted | cloud
AI_EMBEDDINGS_URL=
AI_EMBEDDINGS_MODEL=
AI_EMBEDDINGS_CLOUD_URL=
AI_EMBEDDINGS_CLOUD_MODEL=
AI_INSIGHTS_PROVIDER=spark
AI_INSIGHTS_MODEL=

# Self-hosted embeddings default (empty = not configured, no network call).
SPARK_EMBEDDING_URL=
SPARK_EMBEDDING_MODEL=text-embedding-nomic-embed-text-v1.5

# Ollama Cloud (ollama.com) — insights only.
OLLAMA_CLOUD_URL=https://ollama.com
OLLAMA_CLOUD_INSIGHTS_MODEL=qwen3-coder:480b-cloud
```

Keys are not in this list on purpose — they belong in the settings page, not
in a file.

## Troubleshooting

| Symptom | Check |
|---|---|
| Memory search returns keyword-ish results | **Test embeddings**. If the box is unreachable, new memories are still saved — they just have no vector until the retry loop drains. |
| Test says "no endpoint configured" | Fresh install: fill in the self-hosted URL (or switch to `cloud` with URL + key). Memories saved meanwhile stay vector-less until you run the backfill once: `docker compose exec backend python -m scripts.backfill_memory_embeddings` — there is no automatic backfill. |
| Test reports the wrong dimension | Your model is not a 768-dim model. Either pick one that is, or plan to rebuild the existing vectors. |
| No daily insights report | Provider on `off`? Otherwise check that at least three tasks finished in the window — there is a minimum before MC bothers to summarise. |
| Model browser cannot find a model you can see on the website | It is gated. Accept the licence on huggingface.co, then store a read token here. |
| `401` from ollama.com | Insights runs on `ollama_cloud` but no `ollama_api_key` is stored. |
| `401` from the embeddings cloud host | The `cloud` arm is selected but no `embeddings_cloud_api_key` is stored. |
