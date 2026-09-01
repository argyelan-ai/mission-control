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
});
