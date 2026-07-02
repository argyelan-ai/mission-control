/**
 * Stable hue-from-slug hash for agent identity dots.
 * Returns a CSS hsl() color string. One color per agent slug, consistent
 * across renders and sessions.
 */
export function colorForAgent(slug: string): string {
  if (!slug) return "hsl(260,40%,55%)";
  let hash = 0;
  for (let i = 0; i < slug.length; i++) {
    hash = (hash * 31 + slug.charCodeAt(i)) >>> 0;
  }
  // Use a spread of hues that avoids the brand-purple range (250–280°)
  // so the dot is visually distinct from the accent.
  // Distribute across: 10–230° and 290–350° (skipping 240–280°).
  const hues = [12, 38, 60, 145, 175, 200, 215, 300, 320, 340];
  const hue = hues[hash % hues.length];
  return `hsl(${hue},55%,55%)`;
}
