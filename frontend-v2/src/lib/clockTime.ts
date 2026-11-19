/**
 * "When did this happen" on a card, in the viewer's locale: today → "17:20";
 * another day → "18 Oct, 17:20" / "18. Okt., 17:20". Unparseable → null (the
 * card then shows no time — never an invented one, never "Invalid Date").
 *
 * The one clock format of the app's cards: the runtimes stage's engine death
 * (ADR-096 §11) and the chat's "New conversation started" card (incident
 * 21.10.2026) — the second one had built its own with another English form.
 */
export function formatClockTime(at: string | null | undefined, locale: string, now: Date = new Date()): string | null {
  if (!at) return null;
  const d = new Date(at);
  if (Number.isNaN(d.getTime())) return null;
  const tag = locale === "de" ? "de-CH" : "en-GB";
  const sameDay =
    d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
  return sameDay
    ? d.toLocaleTimeString(tag, { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleString(tag, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}
