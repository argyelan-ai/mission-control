/**
 * The `@` file mention in the composer draft — pure text logic, no UI.
 *
 * A mention is a word that starts with `@` (at the start of the draft or
 * after whitespace) and has no whitespace yet. Choosing a file replaces that
 * word with `@<relative path>` and a space. The path stays plain text in the
 * message: the harness works in the same folder and reads the file itself —
 * MC uploads or attaches nothing for it.
 */

export interface MentionToken {
  /** Index of the `@`. */
  start: number;
  /** Index just after the word the `@` begins. */
  end: number;
  /** What was typed after the `@` up to the caret. */
  query: string;
}

const SPACE = /\s/;

export function findMentionToken(text: string, caret: number): MentionToken | null {
  let start = caret;
  while (start > 0 && !SPACE.test(text[start - 1])) start -= 1;
  if (text[start] !== "@" || start >= caret) return null;
  let end = caret;
  while (end < text.length && !SPACE.test(text[end])) end += 1;
  return { start, end, query: text.slice(start + 1, caret) };
}

/** `@path`; a path with whitespace is quoted so it stays one word. */
export function mentionText(path: string): string {
  return SPACE.test(path) ? `@"${path}"` : `@${path}`;
}

export function insertMention(
  text: string,
  token: MentionToken,
  path: string,
): { text: string; caret: number } {
  const after = text.slice(token.end);
  const mention = mentionText(path);
  const glue = after.length > 0 && SPACE.test(after[0]) ? "" : " ";
  const next = `${text.slice(0, token.start)}${mention}${glue}${after}`;
  return { text: next, caret: token.start + mention.length + (glue ? 1 : 0) };
}
