/**
 * Shared utilities for parsing [A]/[B]/[C] options from agent messages.
 * Used by PlannerPage (wizard) and ChatPage (DM option buttons).
 */

export interface ParsedOption {
  letter: string;
  title: string;
  description: string;
}

export interface ParsedMessage {
  textBefore: string;
  options: ParsedOption[];
}

export function stripMarkdownBold(text: string): string {
  return text.replace(/\*\*(.+?)\*\*/g, "$1").trim();
}

export function parseOptionsFromMessage(content: string): ParsedMessage {
  // Strip <<STAGES: ...>> and <<STAGE: N>> markers (only for step tracking)
  content = content
    .replace(/<<STAGES:\s*.+?>>/g, "")
    .replace(/<<STAGE:\s*\d+\s*>>/g, "")
    .trim();

  // Allows [ A ] with whitespace inside the brackets
  const optionRegex = /^\[\s*([A-D])\s*\]\s+(.+?)(?:\s*[-—–]\s*(.+))?$/gm;
  const options: ParsedOption[] = [];
  let match;

  while ((match = optionRegex.exec(content)) !== null) {
    options.push({
      letter: match[1],
      title: stripMarkdownBold(match[2]),
      description: stripMarkdownBold(match[3] ?? ""),
    });
  }

  if (options.length < 2) return { textBefore: content, options: [] };

  // Extract text before the first option
  const firstOptionMatch = content.match(/\[\s*[A-D]\s*\]/);
  const firstOptionIdx = firstOptionMatch
    ? content.indexOf(firstOptionMatch[0])
    : 0;
  const textBefore = content.slice(0, firstOptionIdx).trim();

  return { textBefore, options };
}
