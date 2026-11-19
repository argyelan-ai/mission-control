import { describe, it, expect } from "vitest";
import { appendQuote, toMarkdownQuote } from "./chatQuote";

describe("toMarkdownQuote", () => {
  it("prefixes every line, keeping blank lines inside the quote", () => {
    expect(toMarkdownQuote("one\n\ntwo")).toBe("> one\n>\n> two");
  });

  it("drops leading and trailing blank lines and CRLF", () => {
    expect(toMarkdownQuote("\n\r\n  first\r\nsecond\n\n")).toBe(">   first\n> second");
  });

  it("is empty for whitespace only", () => {
    expect(toMarkdownQuote(" \n \n")).toBe("");
  });
});

describe("appendQuote", () => {
  it("puts the quote and an empty line into an empty draft", () => {
    expect(appendQuote("", "hi")).toBe("> hi\n\n");
  });

  it("keeps the existing draft and separates it by an empty line", () => {
    expect(appendQuote("my note", "a\nb")).toBe("my note\n\n> a\n> b\n\n");
  });

  it("does not stack blank lines after a draft that already ends in one", () => {
    expect(appendQuote("my note\n\n", "a")).toBe("my note\n\n> a\n\n");
  });

  it("leaves the draft untouched when there is nothing to quote", () => {
    expect(appendQuote("keep", "   ")).toBe("keep");
  });
});
