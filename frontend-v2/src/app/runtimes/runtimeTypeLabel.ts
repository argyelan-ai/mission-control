/**
 * typeLabel — human label for a `runtime_type` value.
 *
 * Extracted from the now-removed SlotStage.tsx (PR 6 "Schliff", v1 retired):
 * `Stage.tsx`, `page.tsx`'s RuntimeRegister and (formerly) `RuntimeDetailPanel`
 * all need the same list, and the label text must not drift between them.
 */
const TYPE_LABELS: Record<string, string> = {
  vllm_docker: "vLLM Docker", lmstudio: "LM Studio", unsloth: "Unsloth",
  unsloth_porsche: "Unsloth · PORSCHE", openai_compatible: "OpenAI-compatible",
  cloud: "Cloud API", hermes: "Hermes", grok: "Grok", kimi: "Kimi",
  omp: "OMP", llamacpp_docker: "llama.cpp", ssh_process: "SSH process",
};

export function typeLabel(t: string): string {
  return TYPE_LABELS[t] ?? t;
}
