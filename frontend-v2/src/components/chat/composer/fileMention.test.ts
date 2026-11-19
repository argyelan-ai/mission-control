import { describe, expect, it } from "vitest";
import { findMentionToken, insertMention, mentionText } from "./fileMention";

describe("findMentionToken", () => {
  it("finds an @ at the start of the draft", () => {
    expect(findMentionToken("@comp", 5)).toEqual({ start: 0, end: 5, query: "comp" });
  });

  it("finds an @ after a space or a new line, with an empty query right after typing it", () => {
    expect(findMentionToken("look at @", 9)).toEqual({ start: 8, end: 9, query: "" });
    expect(findMentionToken("one\n@src/a", 10)).toEqual({ start: 4, end: 10, query: "src/a" });
  });

  it("reads the word up to the caret, and the token ends where the word ends", () => {
    // caret after "@co", the word goes on to "@comp"
    expect(findMentionToken("@comp rest", 3)).toEqual({ start: 0, end: 5, query: "co" });
  });

  it("is no mention inside a word (an e-mail address) or once a space follows", () => {
    expect(findMentionToken("mail me@host", 12)).toBeNull();
    expect(findMentionToken("@comp ", 6)).toBeNull();
    expect(findMentionToken("plain text", 10)).toBeNull();
  });
});

describe("mentionText", () => {
  it("is @path, quoted when the path holds a space", () => {
    expect(mentionText("src/a.ts")).toBe("@src/a.ts");
    expect(mentionText("docs/my notes.md")).toBe('@"docs/my notes.md"');
  });
});

describe("insertMention", () => {
  it("replaces the typed token with the mention plus a space and puts the caret after it", () => {
    const draft = "see @comp";
    const out = insertMention(draft, findMentionToken(draft, 9)!, "src/Composer.tsx");
    expect(out).toEqual({ text: "see @src/Composer.tsx ", caret: 22 });
  });

  it("keeps the text after the token and does not double a space", () => {
    const draft = "@co please";
    const out = insertMention(draft, findMentionToken(draft, 3)!, "a.md");
    expect(out.text).toBe("@a.md please");
    expect(out.caret).toBe(5);
  });
});
