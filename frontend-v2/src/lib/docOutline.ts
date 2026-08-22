/**
 * Gliederung eines Gruppen-Ergebnisdokuments (ADR-075).
 *
 * Das Panel zeigte bis 22.08.2026 den rohen Markdown-Fluss. Ganz oben stand
 * damit erst das Vorspann-Zitat (eine Anweisung an den Lead-Agenten, die den
 * Leser nichts angeht), dann das Ziel, das der Operator selbst getippt hat —
 * und die eigentliche Antwort erst an dritter Stelle, auf dem Handy zwei
 * Bildschirme tief. Operator-Befund: „sehr übersichtlich … es soll wirklich
 * übersichtlicher sein."
 *
 * Deshalb wird hier nicht gerendert, sondern zuerst SORTIERT: Verdikt nach
 * oben, Abschnitte als aufklappbare Einträge, Ziel ans Ende. Bewusst eine
 * reine Funktion ohne React — so ist die Sortierung prüfbar, ohne eine
 * Komponente zu mounten.
 *
 * Kein Markdown-Parser: wir brauchen die Gliederung, nicht den Syntaxbaum.
 * Die einzige Feinheit, die wirklich zählt, sind Code-Blöcke — eine Raute in
 * einem Shell-Beispiel ist keine Überschrift.
 */

export interface DocSection {
  /** Stabil über Neuladen hinweg (Titel + Position) — taugt als React-key
   *  und als Merker, welcher Eintrag offen ist. */
  id: string;
  title: string;
  /** Roher Markdown-Rumpf OHNE die eigene Überschrift; `###` bleibt drin. */
  body: string;
  /** Erste Inhaltszeile, von Markdown-Zeichen befreit. */
  preview: string;
  isGoal: boolean;
}

export interface DocOutline {
  title: string | null;
  /** Das Vorspann-Zitat. Wird nicht im Fluss gezeigt, aber auch nicht
   *  weggeworfen — wer es sehen will, kann es sich holen. */
  note: string | null;
  /** Die Antwort in einem Satz: erster Absatz des ersten Abschnitts, der
   *  nicht das Ziel ist. `null`, wenn es noch keine gibt. */
  lead: string | null;
  sections: DocSection[];
  empty: boolean;
}

const GOAL_TITLE = /^(ziel|goal|auftrag|objective)\b/i;

/** Markdown-Auszeichnung für eine einzeilige Vorschau entfernen. Absichtlich
 *  grob: das Ergebnis ist Text zum Überfliegen, kein Rendering. */
function stripMarkdown(raw: string): string {
  return raw
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, " ")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/^\s{0,3}>\s?/gm, "")
    .replace(/^\s{0,3}#{1,6}\s*/gm, "")
    .replace(/^\s{0,3}[-*+]\s+/gm, "")
    .replace(/(\*\*|__|\*|_|`)/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

/** Erster zusammenhängender Absatz eines Rumpfes, geputzt. */
function firstParagraph(body: string): string {
  const lines = body.split("\n");
  const out: string[] = [];
  let fence = false;
  for (const line of lines) {
    if (/^\s{0,3}```/.test(line)) fence = !fence;
    const empty = !line.trim();
    if (!fence && empty) {
      if (out.length) break;
      continue;
    }
    // Unterüberschriften sind kein Absatz — sie kündigen einen an.
    if (!fence && /^\s{0,3}#{1,6}\s/.test(line)) {
      if (out.length) break;
      continue;
    }
    out.push(line);
  }
  return stripMarkdown(out.join(" "));
}

export function parseDocOutline(markdown: string): DocOutline {
  const text = markdown ?? "";
  if (!text.trim()) {
    return { title: null, note: null, lead: null, sections: [], empty: true };
  }

  const lines = text.split("\n");
  let title: string | null = null;
  const noteLines: string[] = [];
  const raw: { title: string; body: string[] }[] = [];
  let current: { title: string; body: string[] } | null = null;
  let fence = false;
  // Der Vorspann gilt nur, solange noch kein Abschnitt begonnen hat und noch
  // kein normaler Text kam — ein Zitat mitten im Dokument ist Inhalt.
  let inLeadingNote = false;

  for (const line of lines) {
    if (/^\s{0,3}```/.test(line)) fence = !fence;

    if (!fence) {
      const h1 = /^\s{0,3}#\s+(.*)$/.exec(line);
      if (h1 && title === null && current === null) {
        title = h1[1].trim();
        continue;
      }
      const h2 = /^\s{0,3}##\s+(.*)$/.exec(line);
      if (h2) {
        inLeadingNote = false;
        current = { title: h2[1].trim(), body: [] };
        raw.push(current);
        continue;
      }
      if (current === null) {
        const quote = /^\s{0,3}>\s?(.*)$/.exec(line);
        if (quote && (noteLines.length === 0 || inLeadingNote)) {
          inLeadingNote = true;
          noteLines.push(quote[1]);
          continue;
        }
        if (!line.trim()) continue;
        inLeadingNote = false;
      }
    }

    if (current === null) {
      // Text vor dem ersten `##`, der kein Vorspann ist: ein Dokument ganz
      // ohne Gliederung darf nicht verschwinden.
      current = { title: "", body: [] };
      raw.push(current);
    }
    current.body.push(line);
  }

  const sections: DocSection[] = raw
    .map((s, i) => {
      const body = s.body.join("\n").trim();
      return {
        id: `${i}-${s.title || "text"}`,
        title: s.title,
        body,
        preview: firstParagraph(body),
        isGoal: GOAL_TITLE.test(s.title),
      };
    })
    .filter((s) => s.title !== "" || s.body !== "");

  // Ziel ans Ende: der Operator hat es selbst formuliert und liest es nicht
  // noch einmal, wenn er auf den Stand schaut. Innerhalb der beiden Gruppen
  // bleibt die Reihenfolge des Dokuments erhalten.
  const ordered = [...sections.filter((s) => !s.isGoal), ...sections.filter((s) => s.isGoal)];

  const leadSection = ordered.find((s) => !s.isGoal && s.preview);
  return {
    title,
    note: noteLines.length ? stripMarkdown(noteLines.join(" ")) : null,
    lead: leadSection ? leadSection.preview : null,
    sections: ordered,
    empty: false,
  };
}
