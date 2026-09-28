/**
 * One calm paragraph from agent-written text for a clamped preview in the
 * task header (reason, last step, result, error). The full text keeps its
 * markdown in the tabs; a 2–4 line preview would only show the syntax —
 * "## Goal", "**bold**", "- [ ] item" — so it is stripped and blank lines
 * and line breaks collapse into spaces.
 */
export function plainPreview(text: string): string {
  return text
    .replace(/```[a-z0-9_-]*\n?/gi, "") // code fences, keep the code
    .replace(/^\s{0,3}#{1,6}\s+/gm, "") // headings
    .replace(/^\s{0,3}>\s?/gm, "") // quotes
    .replace(/^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?/gm, "") // list markers, task boxes
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1") // links and images → their text
    .replace(/(\*\*|__)(.+?)\1/g, "$2") // bold
    .replace(/(^|[\s(])[*_]([^*_\n]+)[*_](?=[\s).,:;!?]|$)/g, "$1$2") // italic
    .replace(/`([^`]+)`/g, "$1") // inline code
    .replace(/\s+/g, " ")
    .trim();
}
