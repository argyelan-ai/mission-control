# Credits

Mission Control's local-inference recipes stand on the work of a lot of people
outside this project — model labs, quantizers, engine authors, and the
maintainers of the coding harnesses it drives. Thank you.

## Model recipes

Each entry in `backend/config/local-recipes.json` names an external model and,
where it differs, an external engine or quantization. Where the exact
upstream repo is public and pinned in the recipe, it is linked directly.

| Recipe | Model / engine | Author(s) | Link | Licence |
|---|---|---|---|---|
| Qwen3.6 General (Spark) | Qwen3.6 (Alibaba Qwen team), served via [sparkrun](https://github.com/spark-arena/sparkrun) | Alibaba Qwen Team · Spark Arena (sparkrun) | [qwenlm.github.io](https://qwenlm.github.io) · [sparkrun](https://github.com/spark-arena/sparkrun) | Apache 2.0 (Qwen weights) |
| Laguna S21 NVFP4 | Laguna S 2.1 (poolside) | poolside | [poolside.ai](https://poolside.ai) | OpenMDW-1.1 |
| Qwen3 Coder Next 80B-A3B (GB10 NVFP4) | Qwen3-Coder-Next (Alibaba Qwen team), NVFP4 quant | saricles (quant) · Alibaba Qwen Team (model) | [huggingface.co/saricles/Qwen3-Coder-Next-NVFP4-GB10](https://huggingface.co/saricles/Qwen3-Coder-Next-NVFP4-GB10) | Apache 2.0 (base model); quant terms unverified |
| DeepSeek V4 Flash (Spark) | DeepSeek-V4-Flash | DeepSeek | [deepseek.com](https://www.deepseek.com) | MIT |
| Gemma 4 27B NVFP4 | Gemma 4 (google/gemma-4-27b-it) | Google DeepMind | [deepmind.google/technologies/gemma](https://deepmind.google/technologies/gemma/) | Apache 2.0 (Gemma 4) |
| Qwen3.6 27B NVFP4 | Qwen3.6 27B, NVFP4 quant | Unsloth (quant) · Alibaba Qwen Team (model) | [huggingface.co/unsloth/Qwen3.6-27B-NVFP4](https://huggingface.co/unsloth/Qwen3.6-27B-NVFP4) | Apache 2.0 (base model) |
| Qwen3 Embedding 0.6B (GGUF) | Qwen3-Embedding-0.6B, GGUF | Alibaba Qwen Team | [huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF) | Apache 2.0 |
| Qwen3 8B GGUF Q4_K_M | Qwen3-8B, GGUF | Alibaba Qwen Team | [huggingface.co/Qwen/Qwen3-8B-GGUF](https://huggingface.co/Qwen/Qwen3-8B-GGUF) | Apache 2.0 |
| DeepSeek V4 Flash (ds4-server, Spark) | DeepSeek-V4-Flash on **DwarfStar 4 (ds4)** | DeepSeek (model) · Salvatore Sanfilippo / antirez (ds4 engine), packaged for DGX Spark by MiaAI-Lab | [github.com/antirez/ds4](https://github.com/antirez/ds4) · [github.com/MiaAI-Lab/DeepSeek-v4-Flash-One-DGX-Spark](https://github.com/MiaAI-Lab/DeepSeek-v4-Flash-One-DGX-Spark) | MIT (DeepSeek model); ds4 licence unverified — check upstream repo |
| DeepSeek V4 Flash (SparkInfer, Spark) | DeepSeek-V4-Flash, EXL3, on **SparkInfer** (vLLM-based) | DeepSeek (model) · Local Inference Lab (SparkInfer) · 0xSero (Docker recipe) | [github.com/local-inference-lab/sparkinfer](https://github.com/local-inference-lab/sparkinfer) · [github.com/0xSero/deepseek-v4-flash-0731-spark-sparkinfer](https://github.com/0xSero/deepseek-v4-flash-0731-spark-sparkinfer) | MIT (DeepSeek model); SparkInfer/recipe licence unverified |
| GLM-5.3 Flash EXL3 (TensorFold, 2x Spark) | GLM-5.3-Flash (Z.ai/zai-org), EXL3 quant, served via **TensorFold**, speculative drafting via **DFlash2** | Z.ai (model) · ashhart (TensorFold engine) · Mia's AI Lab / MiaAI-Lab (EXL3 quant + DGX-Spark recipe) · Inco AI / incoai (DFlash2 drafter) | [github.com/ashhart/TensorFold](https://github.com/ashhart/TensorFold) · [github.com/MiaAI-Lab/GLM-5.3-Flash-EXL3-2x-DGX-Sparks-TensorFold](https://github.com/MiaAI-Lab/GLM-5.3-Flash-EXL3-2x-DGX-Sparks-TensorFold) · [huggingface.co/incoai/GLM-5.3-Flash-DFlash2](https://huggingface.co/incoai/GLM-5.3-Flash-DFlash2) | **NON-COMMERCIAL ONLY with the default drafter** — DFlash2 is CC BY-NC-ND 4.0; see note below. GLM-5.3-Flash itself is MIT. |

**Note on the GLM-5.3 Flash TensorFold recipe:** the recipe's default drafter
(`incoai/GLM-5.3-Flash-DFlash2`) is licensed **CC BY-NC-ND 4.0** — non-commercial,
no derivatives. As shipped with that drafter, the recipe may only be used
non-commercially. A licence-free alternative exists (`DRAFTER=mtp`, the
checkpoint's own speculative head) at a small speed cost. This is called out
in the recipe's own `notes` field in `local-recipes.json` as well.

## Models

| Model family | Publisher | Licence |
|---|---|---|
| Qwen3 / Qwen3.6 / Qwen3-Coder | Alibaba Cloud — Qwen team ([qwenlm.github.io](https://qwenlm.github.io), [github.com/QwenLM](https://github.com/QwenLM)) | Apache 2.0 |
| GLM-5.3 / GLM-5.3-Flash | Z.ai ([zai-org](https://huggingface.co/zai-org)) | GLM-5.3-Flash: MIT. GLM-5.3 (full): Z.ai's own "GLM-5.3 license" (extra clause for very large hosters) |
| DeepSeek-V4 / DeepSeek-V4-Flash | DeepSeek ([deepseek.com](https://www.deepseek.com)) | MIT |
| Gemma 4 | Google DeepMind ([deepmind.google/technologies/gemma](https://deepmind.google/technologies/gemma/)) | Apache 2.0 |
| Laguna S 2.1 | poolside ([poolside.ai](https://poolside.ai)) | OpenMDW-1.1 |

## Quantizations / engines

| Project | Author(s) | Link | Licence |
|---|---|---|---|
| EXL3 / ExLlamaV3 | turboderp(-org) | [github.com/turboderp-org/exllamav3](https://github.com/turboderp-org/exllamav3) | MIT |
| TensorFold | ashhart | [github.com/ashhart/TensorFold](https://github.com/ashhart/TensorFold) | unverified — check upstream repo for current licence |
| DFlash2 (speculative drafting) | Inco AI (incoai) | [inco.ai/blog/dflash2](https://inco.ai/blog/dflash2/) · [huggingface.co/incoai](https://huggingface.co/incoai) | CC BY-NC-ND 4.0 (non-commercial, no derivatives) — see note above |
| DwarfStar 4 (ds4) | Salvatore Sanfilippo (antirez) | [github.com/antirez/ds4](https://github.com/antirez/ds4) | unverified — check upstream repo |
| SparkInfer | Local Inference Lab | [github.com/local-inference-lab/sparkinfer](https://github.com/local-inference-lab/sparkinfer) | unverified — check upstream repo |
| sparkrun (recipe runner) | Spark Arena | [github.com/spark-arena/sparkrun](https://github.com/spark-arena/sparkrun) | unverified — check upstream repo |
| vLLM | vLLM project / Sky Computing Lab, UC Berkeley | [github.com/vllm-project/vllm](https://github.com/vllm-project/vllm) | Apache 2.0 |
| llama.cpp / GGUF format | Georgi Gerganov and the ggml-org contributors | [github.com/ggml-org/llama.cpp](https://github.com/ggml-org/llama.cpp) | MIT |
| SGLang | SGLang project / LMSYS | [github.com/sgl-project/sglang](https://github.com/sgl-project/sglang) | Apache 2.0 |

## Coding harnesses

Mission Control can drive several third-party coding-agent CLIs as "harnesses" —
the pairing of harness and model runtime is switchable per agent
(ADR-086, `backend/app/services/harness_compat.py`).

| Harness | Publisher | Link | Licence |
|---|---|---|---|
| Claude Code | Anthropic | [anthropic.com/claude-code](https://www.anthropic.com/claude-code) | proprietary (Anthropic) |
| OpenClaude | community (open-source Claude Code–alike, multi-provider) | [github.com/Gitlawb/openclaude](https://github.com/Gitlawb/openclaude) | MIT |
| omp (oh-my-pi) | can1357 / Stencil Labs | [github.com/can1357/oh-my-pi](https://github.com/can1357/oh-my-pi) | MIT |
| Hermes Agent | Nous Research | [github.com/NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) | MIT |
| Kimi Code | Moonshot AI | [github.com/MoonshotAI/kimi-code](https://github.com/MoonshotAI/kimi-code) | per Moonshot AI (check repo) |
| Grok Build (Grok CLI) | xAI | per xAI (official repo) | Apache 2.0 |

## Built with

The platform itself (backend, frontend, infra) is built on:

- [FastAPI](https://fastapi.tiangolo.com/) + [SQLModel](https://sqlmodel.tiangolo.com/) / [SQLAlchemy](https://www.sqlalchemy.org/) — backend API and ORM
- [PostgreSQL](https://www.postgresql.org/) + [Redis](https://redis.io/) — storage and task/pubsub layer
- [Next.js](https://nextjs.org/) + [React](https://react.dev/) — frontend
- [Tailwind CSS](https://tailwindcss.com/) + [Radix UI](https://www.radix-ui.com/) — frontend styling and components
- [xterm.js](https://xtermjs.org/) — in-browser terminal rendering
- [LiveKit](https://livekit.io/) — realtime voice (Jarvis)
- [Remotion](https://www.remotion.dev/) — programmatic video rendering (used by the video/media agent)
- [Docker](https://www.docker.com/) — agent and runtime containers
- [Alembic](https://alembic.sqlalchemy.org/) — database migrations

## Trademarks

NVIDIA, DGX Spark, GB10, and Grace Blackwell are trademarks of NVIDIA
Corporation. Claude and Claude Code are trademarks of Anthropic, PBC. Grok is
a trademark of xAI Corp. Kimi is a trademark of Moonshot AI. Qwen is a
trademark of Alibaba Cloud. Gemma is a trademark of Google LLC. All other
product names, logos, and brands mentioned here are trademarks of their
respective owners. Mission Control is not affiliated with, endorsed by, or
sponsored by any of the organizations named above.
