/**
 * Gruppenchat — Durchgang wie ein Mensch, mit Belegen.
 *
 * Kein Unit-Test: hier wird geklickt, was ein Operator klickt, und nach jedem
 * Schritt ein Screenshot abgelegt. Zweck ist der Wirk-Beweis am laufenden
 * System — Unit-Tests haben bei diesem Feature schon dreimal grün gemeldet,
 * während die Sache live nicht funktionierte (read-only Mount, fehlender
 * CLI-Befehl, Kopf unter der Statusleiste).
 *
 * Aufruf (aus frontend-v2/):  MC_JWT=<token> node playwright/group-walkthrough.mjs
 * Ergebnisse:                 $MC_SHOTS (Standard /tmp/ui-walkthrough) + stdout
 *
 * Der Token kommt wie in `graph-visual.spec.ts` aus der Umgebung — bewusst
 * nicht aus einer Datei: dieses Repo ist öffentlich, ein eingebauter Pfad in
 * eine lokale `.env` wäre sowohl nutzlos für andere als auch verräterisch.
 */
import { chromium, devices } from "playwright";
import fs from "node:fs";
import path from "node:path";

const OUT = process.env.MC_SHOTS ?? "/tmp/ui-walkthrough";
const TOKEN = process.env.MC_JWT ?? "";

if (!TOKEN) {
  console.error("MC_JWT fehlt — ohne Token zeigt die Seite nur den Login.");
  process.exit(2);
}

fs.mkdirSync(OUT, { recursive: true });

const findings = [];
const ok = (schritt, detail) => findings.push({ ergebnis: "OK", schritt, detail });
const fehler = (schritt, detail) => findings.push({ ergebnis: "FEHLER", schritt, detail });

async function shot(page, name) {
  await page.screenshot({ path: path.join(OUT, `${name}.png`) });
}

async function api(page, pfad) {
  return page.evaluate(async (p) => {
    const r = await fetch(p, {
      headers: { Authorization: "Bearer " + localStorage.getItem("mc_auth_token") },
    });
    return r.ok ? r.json() : null;
  }, pfad);
}

async function durchgang(browser, label, deviceOpts) {
  const ctx = await browser.newContext(deviceOpts);
  const page = await ctx.newPage();
  await page.addInitScript((t) => localStorage.setItem("mc_auth_token", t), TOKEN);

  // 1. Sessions-Seite öffnen — steht die Gruppen-Sektion oben?
  await page.goto("http://localhost/sessions", { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(3500);
  await shot(page, `${label}-1-liste`);

  const groups = await api(page, "/api/v1/groups");
  if (!groups?.length) {
    fehler(`${label}: Gruppen vorhanden`, "Die API liefert keine Gruppe — Durchgang nicht möglich");
    await ctx.close();
    return;
  }
  ok(`${label}: Gruppen vorhanden`, `${groups.length} Gruppen`);

  const sektion = await page.evaluate(() => {
    const box = document.querySelector('[role="listbox"][aria-label="Sessions"]');
    const text = box?.textContent ?? "";
    const i = text.search(/Gruppen|Groups/);
    return { gefunden: i >= 0, position: i, anfang: text.slice(0, 60) };
  });
  sektion.gefunden
    ? ok(`${label}: Sektion GRUPPEN sichtbar`, `an Position ${sektion.position} der Liste`)
    : fehler(`${label}: Sektion GRUPPEN sichtbar`, `nicht gefunden, Liste beginnt mit: ${sektion.anfang}`);

  // 2. Eine Gruppe öffnen — wie ein Mensch: anklicken, nicht per URL springen
  // `:visible` ist hier nicht Kosmetik: die Sidebar rendert mehrere Varianten
  // (Schiene, Liste, Sheet) und blendet die unpassenden per `md:`-Klassen aus.
  // Ohne den Filter klickt der Test auf eine Zeile, die ein Mensch gar nicht
  // sehen kann — und wartet 30 s auf ein Element, das nie sichtbar wird.
  const zeile = page.locator('[role="option"]:visible').first();
  if (await zeile.count()) {
    await zeile.click();
    await page.waitForTimeout(2500);
  } else {
    await page.goto(`http://localhost/sessions?group=${groups[0].id}`, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(3000);
    fehler(`${label}: Gruppe per Klick öffnen`, "Keine anklickbare Zeile gefunden — per URL geöffnet");
  }
  await shot(page, `${label}-2-raum`);

  const raum = await page.evaluate(() => {
    const view = document.querySelector('[data-testid="group-chat-view"]');
    const header = view?.firstElementChild;
    const msgs = document.querySelector('[data-testid="group-messages"]');
    return {
      raumDa: !!view,
      kopfPaddingTop: header ? getComputedStyle(header).paddingTop : null,
      seiteScrollt: document.body.scrollHeight > window.innerHeight + 2,
      verlaufScrollt: msgs ? msgs.scrollHeight > msgs.clientHeight : null,
      zielSichtbar: /Ziel|Goal/i.test(header?.textContent ?? ""),
    };
  });
  raum.raumDa ? ok(`${label}: Raum geöffnet`, "") : fehler(`${label}: Raum geöffnet`, "kein group-chat-view");
  raum.zielSichtbar
    ? ok(`${label}: Ziel im Kopf sichtbar`, "")
    : fehler(`${label}: Ziel im Kopf sichtbar`, "keine Ziel-Zeile gefunden");
  raum.seiteScrollt
    ? fehler(`${label}: nur der Verlauf scrollt`, "die ganze Seite scrollt — Kopf wandert weg")
    : ok(`${label}: nur der Verlauf scrollt`, `Verlauf scrollbar: ${raum.verlaufScrollt}`);
  if (label === "mobil") {
    // Auf dem Handy laeuft die Seite chromelos; die Kopfzeile MUSS die
    // Statusleiste selbst abfedern (pt-safe-top).
    raum.kopfPaddingTop && parseFloat(raum.kopfPaddingTop) > 0
      ? ok("mobil: Kopf federt Statusleiste ab", `padding-top ${raum.kopfPaddingTop}`)
      : fehler("mobil: Kopf federt Statusleiste ab", `padding-top ${raum.kopfPaddingTop}`);
  }

  // 3. Maschinen-Auftrag: zugeklappt? aufklappbar?
  const sysZu = await page.locator('[data-testid="group-system-toggle"]').count();
  const sysOffen = await page.locator('[data-testid="group-system-body"]').count();
  if (sysZu > 0) {
    ok(`${label}: Runden-Briefe zugeklappt`, `${sysZu} Aufklapper, ${sysOffen} offen`);
    await page.locator('[data-testid="group-system-toggle"]').first().click();
    await page.waitForTimeout(600);
    const jetztOffen = await page.locator('[data-testid="group-system-body"]').count();
    jetztOffen > sysOffen
      ? ok(`${label}: Auftrag lässt sich aufklappen`, "")
      : fehler(`${label}: Auftrag lässt sich aufklappen`, "Klick zeigte nichts");
    await shot(page, `${label}-3-auftrag-offen`);
    await page.locator('[data-testid="group-system-toggle"]').first().click();
    await page.waitForTimeout(400);
  } else {
    fehler(`${label}: Runden-Briefe zugeklappt`, "kein einziger Aufklapper im Raum");
  }

  // 4. Lange Beiträge: geklemmt? aufklappbar?
  const beitragZu = await page.locator('[data-testid="group-contribution-toggle"]').count();
  if (beitragZu > 0) {
    ok(`${label}: lange Beiträge geklemmt`, `${beitragZu} Aufklapper`);
    const vorher = await page.locator('[data-clamped="true"]').count();
    await page.locator('[data-testid="group-contribution-toggle"]').first().click();
    await page.waitForTimeout(600);
    const nachher = await page.locator('[data-clamped="true"]').count();
    nachher < vorher
      ? ok(`${label}: Beitrag lässt sich aufklappen`, `${vorher} → ${nachher} geklemmt`)
      : fehler(`${label}: Beitrag lässt sich aufklappen`, "Klemme blieb bestehen");
    await shot(page, `${label}-4-beitrag-offen`);
  } else {
    ok(`${label}: lange Beiträge geklemmt`, "kein Beitrag über der Schwelle (nach der Kursänderung erwartbar)");
  }

  // 5. Ergebnis-Dokument erreichbar?
  const ergebnisKnopf = page.locator('button[aria-label="Ergebnis"], button[aria-label="Result"]');
  if (await ergebnisKnopf.count()) {
    await ergebnisKnopf.first().click();
    await page.waitForTimeout(1800);
    const inhalt = await page.evaluate(() => document.body.innerText.length);
    ok(`${label}: Ergebnis-Panel öffnet`, `${inhalt} Zeichen auf der Seite`);
    await shot(page, `${label}-5-ergebnis`);
  } else {
    fehler(`${label}: Ergebnis-Panel öffnet`, "kein Ergebnis-Knopf gefunden");
  }

  // 6. Composer da und benutzbar?
  const feld = page.locator('textarea').first();
  if (await feld.count()) {
    await feld.fill("@alle Testeingabe (wird nicht gesendet)");
    await page.waitForTimeout(400);
    const wert = await feld.inputValue();
    wert.includes("Testeingabe")
      ? ok(`${label}: Eingabefeld nimmt Text an`, "")
      : fehler(`${label}: Eingabefeld nimmt Text an`, `Wert: ${wert}`);
    await feld.fill("");
  } else {
    fehler(`${label}: Eingabefeld vorhanden`, "keine textarea gefunden");
  }

  await ctx.close();
}

const browser = await chromium.launch();
await durchgang(browser, "mobil", { ...devices["iPhone 13 Pro"] });
await durchgang(browser, "desktop", { viewport: { width: 1440, height: 900 } });
await browser.close();

console.log("\n=== UI-DURCHGANG ===");
for (const f of findings) {
  console.log(`${f.ergebnis === "OK" ? "✓" : "✗"} ${f.schritt}${f.detail ? " — " + f.detail : ""}`);
}
const fehlerAnzahl = findings.filter((f) => f.ergebnis === "FEHLER").length;
console.log(`\n${findings.length - fehlerAnzahl} von ${findings.length} Prüfungen bestanden.`);
console.log(`Screenshots: ${OUT}`);
process.exit(fehlerAnzahl > 0 ? 1 : 0);
