// The prompt library's search + tag filter, shared by the library page (P8)
// and the test sheet's prompt picker (P3) — one rule, so both find the same
// prompts. Same data, same cache: PROMPT_TEMPLATES_KEY + benchApi.promptTemplates.
import type { PromptTemplate } from "./types";

export const PROMPT_TEMPLATES_KEY = ["prompt-templates"] as const;

/** Every tag once, sorted. */
export function templateTags(templates: PromptTemplate[] | undefined): string[] {
  return Array.from(new Set((templates ?? []).flatMap((t) => t.tags ?? []))).sort();
}

/** Search in title + body (case-insensitive), narrowed to one tag. */
export function filterTemplates(
  templates: PromptTemplate[] | undefined,
  search: string,
  tag: string | null,
): PromptTemplate[] {
  const q = search.trim().toLowerCase();
  return (templates ?? []).filter((t) => {
    const matchesSearch = !q || t.title.toLowerCase().includes(q) || t.body.toLowerCase().includes(q);
    const matchesTag = !tag || (t.tags ?? []).includes(tag);
    return matchesSearch && matchesTag;
  });
}
