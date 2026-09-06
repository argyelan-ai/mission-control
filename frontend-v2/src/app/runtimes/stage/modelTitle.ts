/**
 * Kurzname-Regeln für die Bühne v2 (Live-Sichtprüfung 06.09.2026): der volle
 * `runtime.display_name` trägt Engine/Quant/Topologie
 * ("Qwen3.8 Flash Next NVFP4 MTP3 — vLLM (2× Spark)") — auf der Karte reicht
 * der Modellname, der Rest gehört ins Cockpit (PR 5). Der volle Name bleibt
 * als `title`-Attribut erreichbar, nichts geht verloren.
 */

// Quant-/Engine-/Topologie-Tokens, die am ENDE des Kurznamens abgeschnitten
// werden, iterativ (mehrere Tokens hintereinander, z.B. "NVFP4 MTP3").
// Reihenfolge egal — jede Runde entfernt genau ein Token vom Ende.
const TRAILING_TOKEN = /\s+(NVFP4|NVFP8|MTP\d*|EXL\d+|\d+bpw|AWQ|FP\d+|BF16|GGUF|INT\d+|Q\d+(_[A-Z0-9]+)?)$/i;

// „(2× Spark)"/„(1× Spark)"/„(Duo)" — Boxen-Anzahl gehört zur Mitglieder-Zone,
// nicht in den Titel.
const TRAILING_PAREN = /\s*\([^()]*\)\s*$/;

/**
 * Kurzname für Zone 1 (Spec §2 Lebenszeichen). Nimmt alles vor dem ersten
 * Gedankenstrich („ — "), entfernt einen trailing Klammer-Zusatz und
 * schneidet iterativ bekannte Quant-/Engine-Suffixe vom Ende.
 */
export function shortModelTitle(fullName: string): string {
  let name = fullName.split(" — ")[0].trim();
  name = name.replace(TRAILING_PAREN, "").trim();
  // eslint-disable-next-line no-constant-condition
  while (true) {
    const next = name.replace(TRAILING_TOKEN, "").trim();
    if (next === name || next.length === 0) break;
    name = next;
  }
  return name || fullName;
}

/**
 * Kurzname für Zone 3 „Mitglieder" (Live-Sichtprüfung 06.09.2026): der
 * Backend-Anzeigename kann den technischen Hostnamen als Klammer-Zusatz
 * tragen ("GX10 (gx10-dd72)") — die Karte zeigt nur den Namen, der Rest ist
 * Cockpit-Detail (Connection/Recipe-Gruppe, PR 5).
 */
export function shortHostName(displayName: string): string {
  const idx = displayName.indexOf(" (");
  return idx === -1 ? displayName : displayName.slice(0, idx).trim();
}
