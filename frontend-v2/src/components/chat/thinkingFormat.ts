/**
 * Small formatters for the thinking rows (20.10.2026).
 *
 * `formatElapsed`: a running or finished thought's duration as a clock
 * (`1:45`, `1:02:03`) — K10: one fixed form, tabular figures at the caller.
 *
 * `estimateTokens`: ACP streams thought TEXT, not token counts, so the chat
 * can only estimate (≈4 characters per token for the models we run). The
 * result is rounded (tens below 1 000, hundreds above) and always shown with
 * "≈" — a precise-looking 5 912 would claim a count nobody made.
 */

export function formatElapsed(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const ss = String(s).padStart(2, "0");
  return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}

export function estimateTokens(chars: number): number {
  const raw = Math.max(0, chars) / 4;
  if (raw <= 0) return 0;
  const step = raw < 1000 ? 10 : 100;
  return Math.max(step, Math.round(raw / step) * step);
}
