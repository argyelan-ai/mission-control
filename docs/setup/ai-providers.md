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

| Provider | Means | Needs a key |
|---|---|---|
| `spark` | **Your own GPU box** — any OpenAI-compatible endpoint (vLLM, LM Studio, llama.cpp). The default. | No — it's your machine |
| `ollama_cloud` | **ollama.com**, the hosted Ollama service | Yes, an Ollama Cloud key |
| `off` | Insights only: no report is generated | — |

> **Ollama Cloud means ollama.com, not a local Ollama.** Running Ollama
> locally next to MC is deliberately not an option here. On Apple Silicon it
> competes with Docker for the same unified memory and can take the whole
> machine down. If you want local inference, run it on a separate box and
> point `spark` at it — that is exactly what the `spark` provider is for.

## 1. Embeddings

Pick the provider, and optionally pin an endpoint and a model:

```
AI_EMBEDDINGS_PROVIDER=spark        # spark | ollama_cloud
AI_EMBEDDINGS_URL=                  # empty = the provider's default
AI_EMBEDDINGS_MODEL=                # empty = the provider's default
```

With `spark` and both overrides empty, MC uses `SPARK_EMBEDDING_URL` and
`SPARK_EMBEDDING_MODEL` — i.e. exactly what it did before this page existed.

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

Both keys are stored encrypted in MC's vault, never in `.env`, and are only
ever read by the functions above:

| Key | Used by |
|---|---|
| `hf_token` | Model browser (search, files, download) |
| `ollama_api_key` | Embeddings and/or insights, when set to `ollama_cloud` |

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
AI_EMBEDDINGS_PROVIDER=spark
AI_EMBEDDINGS_URL=
AI_EMBEDDINGS_MODEL=
AI_INSIGHTS_PROVIDER=spark
AI_INSIGHTS_MODEL=

# Ollama Cloud (ollama.com) — only used when a provider above is ollama_cloud.
OLLAMA_CLOUD_URL=https://ollama.com
OLLAMA_CLOUD_EMBEDDING_MODEL=nomic-embed-text
OLLAMA_CLOUD_INSIGHTS_MODEL=qwen3-coder:480b-cloud
```

Keys are not in this list on purpose — they belong in the settings page, not
in a file.

## Troubleshooting

| Symptom | Check |
|---|---|
| Memory search returns keyword-ish results | **Test embeddings**. If the box is unreachable, new memories are still saved — they just have no vector until the retry loop drains. |
| Test reports the wrong dimension | Your model is not a 768-dim model. Either pick one that is, or plan to rebuild the existing vectors. |
| No daily insights report | Provider on `off`? Otherwise check that at least three tasks finished in the window — there is a minimum before MC bothers to summarise. |
| Model browser cannot find a model you can see on the website | It is gated. Accept the licence on huggingface.co, then store a read token here. |
| `401` from ollama.com | A cloud provider is selected but no `ollama_api_key` is stored. |
