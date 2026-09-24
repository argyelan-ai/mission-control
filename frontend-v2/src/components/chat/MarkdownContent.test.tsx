import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { MarkdownContent } from "./MarkdownContent";

/**
 * Operator-Befund 01.09.2026: "Tabellen werden nicht korrekt gerendert" und
 * "Ueberschriften bitte farblich unterscheiden wie in Claude Code".
 *
 * Tabellen sind GitHub-Markdown (GFM) — ohne das Plugin gibt react-markdown
 * die Pipe-Zeilen als Fliesstext aus. Genau das passierte hier.
 */
describe("MarkdownContent", () => {
  it("rendert eine Markdown-Tabelle als Tabelle", () => {
    const md = [
      "| Modell | Tempo |",
      "| --- | --- |",
      "| GLM | 36 t/s |",
    ].join("\n");

    const { container } = render(<MarkdownContent content={md} />);

    expect(container.querySelector("table")).not.toBeNull();
    expect(container.querySelectorAll("th")).toHaveLength(2);
    expect(container.querySelectorAll("td")).toHaveLength(2);
    expect(screen.getByText("36 t/s")).toBeTruthy();
  });

  it("rendert durchgestrichenen Text und Aufgabenlisten (GFM)", () => {
    const { container } = render(
      <MarkdownContent content={"~~weg~~\n\n- [x] erledigt\n- [ ] offen"} />,
    );
    expect(container.querySelector("del")).not.toBeNull();
    expect(container.querySelectorAll('input[type="checkbox"]').length).toBe(2);
  });

  it("faerbt Ueberschriften-Ebenen unterschiedlich", () => {
    const { container } = render(
      <MarkdownContent content={"# Eins\n\n## Zwei\n\n### Drei"} />,
    );
    const colors = ["h1", "h2", "h3"].map(
      (t) => (container.querySelector(t) as HTMLElement).style.color,
    );
    expect(new Set(colors).size).toBe(3);
    expect(colors.every((c) => c.length > 0)).toBe(true);
  });

  // Operator-Befund 19.09.2026 (iPhone, nach #634): ein gefencer Block OHNE
  // Language-Tag lief bisher in den Inline-Zweig — kein eigener Scroller, und
  // `white-space: pre` im <pre> verhindert jeden Umbruch, selbst bei Zeilen
  // nur mit Leerzeichen. Das <pre> wurde 194px breiter als das Transkript und
  // schob den ganzen Chat seitwaerts (playwright/chat-transcript-width.mjs
  // misst das Layout; dieser Test pinnt den Contract an der Komponente).
  it("gibt gefencen Bloecken ohne Language-Tag einen eigenen Scroller aufs pre", () => {
    const { container } = render(
      <MarkdownContent content={"```\ndocker compose --profile n-control build --no-cache frontend\n```"} />,
    );
    const pre = container.querySelector("pre");
    expect(pre).not.toBeNull();
    expect(pre?.className).toContain("overflow-x-auto");
    // Das <code> im Block rendert ohne Inline-Pill-Hintergrund — der Kasten
    // ist das <pre>, nicht die Zeile.
    const code = pre?.querySelector("code");
    expect(code?.className).not.toContain("rounded-sm");
  });
});
