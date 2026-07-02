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
 * Dynamischer Comment-Parser.
 *
 * Erkennt JEDES Pattern mit **Label:** oder **Label** gefolgt von Inhalt.
 * Kein hardcoded Label-Set mehr — alles was in **...** steht wird als
 * Section-Label behandelt.
 *
 * Checklist-Zeilen (- [x] / - [ ]) werden als eigene "Checklist"-Section
 * extrahiert wenn sie ausserhalb von bold-Sections stehen.
 */
export function parseComment(content: string, _commentType?: string): ParsedComment {
  if (!content.trim()) {
    return { type: "plain", sections: [], raw: content };
  }

  // Regex: Zeilenanfang (optional whitespace), dann **Label** gefolgt von
  // optionalem : oder — oder einfach Whitespace, dann Rest der Zeile.
  // Multiline-Flag: jede Zeile wird geprüft.
  const labelPattern = /^\s*\*\*([^*]+)\*\*\s*(?:[:—–\-]\s*)?/gm;

  // Alle Label-Positionen sammeln
  const matches: Array<{ label: string; matchEnd: number; startIndex: number }> = [];
  let match: RegExpExecArray | null;

  while ((match = labelPattern.exec(content)) !== null) {
    const label = match[1].trim();
    // Leere Labels oder Labels die nur Sonderzeichen sind ignorieren
    if (!label || /^[\s*_~`]+$/.test(label)) continue;

    matches.push({
      label,
      matchEnd: match.index + match[0].length,
      startIndex: match.index,
    });
  }

  if (matches.length === 0) {
    // Checklist-only check: wenn der Content nur Checklist-Zeilen hat
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

  // Content vor dem ersten Match als "Intro" aufnehmen (wenn vorhanden)
  if (matches[0].startIndex > 0) {
    const intro = content.slice(0, matches[0].startIndex).trim();
    if (intro) {
      sections.push({ label: "Intro", content: intro });
    }
  }

  // Sections extrahieren: Content geht vom Ende des Label-Match
  // bis zum Start des nächsten Label-Match (oder Ende des Strings)
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
