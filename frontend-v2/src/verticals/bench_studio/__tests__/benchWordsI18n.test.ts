import { describe, it, expect } from "vitest";
import de from "../../../../messages/de.json";
import en from "../../../../messages/en.json";

// P4 "one word per action" (t2 SOLL-3 / r2 S4), review SOLL-2: the button says
// "Entwurf erstellen" and the entry pill says "Direkt-API" — the dialog behind
// the button and the source picker in "Neue Challenge" must use the same words.

describe("bench words — one word per action (de)", () => {
  it("the draft dialog says 'Entwurf', never 'Draft'", () => {
    for (const [key, value] of Object.entries(de.bench.draftDialog)) {
      expect(value, `bench.draftDialog.${key}`).not.toMatch(/\bDraft\b/);
    }
    expect(de.bench.draftDialog.createDraft).toBe(de.bench.challengeDetail.draft);
  });

  it("the source picker says the same as the entry pill", () => {
    expect(de.bench.newChallengeDialog.sourceSpark).toBe(de.bench.challengeDetail.directApiPill);
    expect(en.bench.newChallengeDialog.sourceSpark).toBe(en.bench.challengeDetail.directApiPill);
  });

  it("en: the next-step button and the dialog submit read the same", () => {
    expect(en.bench.draftDialog.createDraft).toBe(en.bench.challengeDetail.draft);
  });
});

// Round-4 K1: the field messages and the tag placeholder exist in both
// languages; German says the spark default in German (r2 N7 — it read
// "Tag (LOCAL · DIRECT API)").
describe("bench dialog — field messages and tag placeholder (round-4 K1)", () => {
  it("de and en carry the same new-challenge keys", () => {
    const keys = (o: object) => JSON.stringify(Object.keys(o).sort());
    expect(keys(de.bench.newChallengeDialog)).toBe(keys(en.bench.newChallengeDialog));
    expect(keys(de.bench.newChallengeDialog.fieldError)).toBe(keys(en.bench.newChallengeDialog.fieldError));
  });

  it("de: no English capitals in the tag placeholders, a German limit message", () => {
    const d = de.bench.newChallengeDialog;
    for (const v of [d.tagPlaceholder, d.tagPlaceholderSpark, d.tagPlaceholderAgent]) {
      expect(v).not.toMatch(/LOCAL|DIRECT|empty/);
    }
    expect(d.fieldError.tooLong).toBe("Höchstens {max} Zeichen.");
  });
});
