# Credits

Mission Control's local-inference recipes stand on the work of a lot of people
outside this project — model labs, quantizers, engine authors, and the
maintainers of the coding harnesses it drives. Thank you.

## Model recipes

Each recipe in `backend/config/local-recipes.json` names an external model, quantization and engine. Where the exact
upstream repo is public and pinned in the recipe, it is linked directly.

| Recipe | Model / engine | Author(s) | Link | Licence |
|---|---|---|---|---|
| GLM-5.3 Flash EXL3 (TensorFold, 2x Spark) | GLM-5.3-Flash (Z.ai/zai-org), EXL3 quant, served via **TensorFold**, speculative drafting via **DFlash2** | Z.ai (model) · ashhart (TensorFold engine) · Mia's AI Lab / MiaAI-Lab (EXL3 quant + DGX-Spark recipe) · Inco AI / incoai (DFlash2 drafter) | [github.com/ashhart/TensorFold](https://github.com/ashhart/TensorFold) · [github.com/MiaAI-Lab/GLM-5.3-Flash-EXL3-2x-DGX-Sparks-TensorFold](https://github.com/MiaAI-Lab/GLM-5.3-Flash-EXL3-2x-DGX-Sparks-TensorFold) · [huggingface.co/incoai/GLM-5.3-Flash-DFlash2](https://huggingface.co/incoai/GLM-5.3-Flash-DFlash2) | **NON-COMMERCIAL ONLY with the default drafter** — DFlash2 is CC BY-NC-ND 4.0; see note below. GLM-5.3-Flash itself is MIT. |
| DeepSeek-V4.1 Flash 1017 EXL3 (TensorFold, 2x Spark) | DeepSeek-V4.1-Flash-1017 (DeepSeek), EXL3 EAQ v1 2.9 bpw quant, served via **TensorFold**'s native Zig engine (the `dsv41` family), speculative decoding from the model's own DSpark draft blocks; optional abliteration overlay | DeepSeek (model, Engram tables, DSpark blocks) · Mia's AI Lab / MiaAI-Lab (EXL3 quant + DGX-Spark recipe) · ashhart and the TensorFold contributors (engine) · BertholomusAI (`dsv41` family, inputs' tools, kernel kit) · drowzeys (optional abliteration overlay) | [github.com/MiaAI-Lab/DeepSeek-v4.1-Flash-1017-2x-DGX-Sparks](https://github.com/MiaAI-Lab/DeepSeek-v4.1-Flash-1017-2x-DGX-Sparks) · [huggingface.co/Mia-AiLab/DeepSeek-V4.1-Flash-1017-EXL3-EAQ-v1-2.9-bpw](https://huggingface.co/Mia-AiLab/DeepSeek-V4.1-Flash-1017-EXL3-EAQ-v1-2.9-bpw) · [huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash-1017](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash-1017) · [github.com/bertholomus/TensorFold](https://github.com/bertholomus/TensorFold) · [huggingface.co/drowzeys/keys-DeepSeek-V4.1-Flash-1017-Abliterated](https://huggingface.co/drowzeys/keys-DeepSeek-V4.1-Flash-1017-Abliterated) (gated) | Permissive: recipe, TensorFold, BertholomusAI's fork and kit Apache-2.0; DeepSeek weights, Mia's quant and the overlay MIT. Image base: NVIDIA PyTorch container (NVIDIA's container licence). |

**Note on the GLM-5.3 Flash TensorFold recipe:** the recipe's default drafter
(`incoai/GLM-5.3-Flash-DFlash2`) is licensed **CC BY-NC-ND 4.0** — non-commercial,
no derivatives. As shipped with that drafter, the recipe may only be used
non-commercially. A licence-free alternative exists (`DRAFTER=mtp`, the
checkpoint's own speculative head) at a small speed cost. This is called out
in the recipe's own `notes` field in `local-recipes.json` as well.

## Models

| Model family | Publisher | Licence |
|---|---|---|
| GLM-5.3 / GLM-5.3-Flash | Z.ai ([zai-org](https://huggingface.co/zai-org)) | GLM-5.3-Flash: MIT. GLM-5.3 (full): Z.ai's own "GLM-5.3 license" (extra clause for very large hosters) |
| DeepSeek-V4.1-Flash-1017 | DeepSeek ([deepseek-ai](https://huggingface.co/deepseek-ai)) | MIT |

## Quantizations / engines

| Project | Author(s) | Link | Licence |
|---|---|---|---|
| EXL3 / ExLlamaV3 | turboderp(-org) | [github.com/turboderp-org/exllamav3](https://github.com/turboderp-org/exllamav3) | MIT |
| TensorFold | ashhart and the TensorFold contributors | [github.com/ashhart/TensorFold](https://github.com/ashhart/TensorFold) | Apache 2.0 |
| TensorFold `dsv41` family (DeepSeek-V4.1 on TensorFold's Zig engine), inputs' tools, kernel kit | BertholomusAI (bertholomus) | [github.com/bertholomus/TensorFold](https://github.com/bertholomus/TensorFold) · [github.com/bertholomus/deepseek-v4.1-tensorfold-tp2-2xgb10](https://github.com/bertholomus/deepseek-v4.1-tensorfold-tp2-2xgb10) | Apache 2.0 (the kit's parts keep theirs: Triton MIT, PyTorch/CUTLASS BSD-3-Clause) |
| DeepSeek-V4.1-Flash-1017 EXL3 EAQ v1 2.9 bpw | Mia's AI Lab (Mia-AiLab) | [huggingface.co/Mia-AiLab/DeepSeek-V4.1-Flash-1017-EXL3-EAQ-v1-2.9-bpw](https://huggingface.co/Mia-AiLab/DeepSeek-V4.1-Flash-1017-EXL3-EAQ-v1-2.9-bpw) | MIT |
| Keys abliteration overlay (optional, `ABLIT=1`) | drowzeys | [huggingface.co/drowzeys/keys-DeepSeek-V4.1-Flash-1017-Abliterated](https://huggingface.co/drowzeys/keys-DeepSeek-V4.1-Flash-1017-Abliterated) | MIT; gated repo (needs a Hugging Face token with access) |
| DFlash2 (speculative drafting) | Inco AI (incoai) | [inco.ai/blog/dflash2](https://inco.ai/blog/dflash2/) · [huggingface.co/incoai](https://huggingface.co/incoai) | CC BY-NC-ND 4.0 (non-commercial, no derivatives) — see note above |
| vLLM | vLLM project / Sky Computing Lab, UC Berkeley | [github.com/vllm-project/vllm](https://github.com/vllm-project/vllm) | Apache 2.0 |

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
