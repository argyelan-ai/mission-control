export interface CommentSection {
  label: string;
  content: string;
}

export interface ParsedComment {
  type: "structured" | "plain";
  sections: CommentSection[];
  raw: string;
}

/**
 * Dynamic comment parser.
 *
 * Recognizes ANY pattern with **Label:** or **Label** followed by content.
 * No more hardcoded label set — anything inside **...** is treated as
 * a section label.
 *
 * Checklist lines (- [x] / - [ ]) are extracted as their own "Checklist"
 * section when they sit outside bold sections.
 */
export function parseComment(content: string, _commentType?: string): ParsedComment {
  if (!content.trim()) {
    return { type: "plain", sections: [], raw: content };
  }

  // Regex: start of line (optional whitespace), then **Label** followed by
  // an optional : or — or plain whitespace, then the rest of the line.
  // Multiline flag: every line is checked.
  const labelPattern = /^\s*\*\*([^*]+)\*\*\s*(?:[:—–\-]\s*)?/gm;

  // Collect all label positions
  const matches: Array<{ label: string; matchEnd: number; startIndex: number }> = [];
  let match: RegExpExecArray | null;

  while ((match = labelPattern.exec(content)) !== null) {
    const label = match[1].trim();
    // Ignore empty labels or labels that consist only of special characters
    if (!label || /^[\s*_~`]+$/.test(label)) continue;

    matches.push({
      label,
      matchEnd: match.index + match[0].length,
      startIndex: match.index,
    });
  }

  if (matches.length === 0) {
    // Checklist-only check: when the content has only checklist lines
    const checklistLines = content.split("\n").filter((l) =>
      /^\s*-\s*\[(x| )\]/i.test(l)
    );
    if (checklistLines.length > 0) {
      return {
        type: "structured",
        sections: [{ label: "Checklist", content: content.trim() }],
        raw: content,
      };
    }

    return { type: "plain", sections: [], raw: content };
  }

  const sections: CommentSection[] = [];

  // Capture content before the first match as "Intro" (if present)
  if (matches[0].startIndex > 0) {
    const intro = content.slice(0, matches[0].startIndex).trim();
    if (intro) {
      sections.push({ label: "Intro", content: intro });
    }
  }

  // Extract sections: content runs from the end of the label match
  // to the start of the next label match (or the end of the string)
  for (let i = 0; i < matches.length; i++) {
    const m = matches[i];
    const contentEnd = i + 1 < matches.length
      ? matches[i + 1].startIndex
      : content.length;

    const sectionContent = content.slice(m.matchEnd, contentEnd).trim();
    sections.push({ label: m.label, content: sectionContent });
  }

  if (sections.length === 0) {
    return { type: "plain", sections: [], raw: content };
  }

  return { type: "structured", sections, raw: content };
}
