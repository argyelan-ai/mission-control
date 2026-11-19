import { describe, expect, it } from "vitest";
import type { PromptTemplate } from "../types";
import { filterTemplates, templateTags } from "../promptFilter";

const tpl = (id: string, title: string, body: string, tags: string[]): PromptTemplate => ({
  id,
  title,
  body,
  tags,
  created_at: "2026-07-11T09:00:00Z",
  updated_at: "2026-07-11T09:00:00Z",
});

const LIB = [
  tpl("a", "Pagoda Garden", "A pagoda in a garden, three.js", ["3d", "scene"]),
  tpl("b", "Bouncing balls", "100 balls in one index.html", ["physics", "animation"]),
  tpl("c", "Snake", "a snake game", ["game", "animation"]),
];

describe("prompt library filter (library tab + test sheet share it)", () => {
  it("collects every tag once, sorted", () => {
    expect(templateTags(LIB)).toEqual(["3d", "animation", "game", "physics", "scene"]);
    expect(templateTags(undefined)).toEqual([]);
  });

  it("matches the search in title or body, ignoring case and outer spaces", () => {
    expect(filterTemplates(LIB, "  PAGODA ", null).map((t) => t.id)).toEqual(["a"]);
    expect(filterTemplates(LIB, "index.html", null).map((t) => t.id)).toEqual(["b"]);
    expect(filterTemplates(LIB, "", null).map((t) => t.id)).toEqual(["a", "b", "c"]);
  });

  it("narrows to one tag, and search + tag together", () => {
    expect(filterTemplates(LIB, "", "animation").map((t) => t.id)).toEqual(["b", "c"]);
    expect(filterTemplates(LIB, "snake", "animation").map((t) => t.id)).toEqual(["c"]);
    expect(filterTemplates(LIB, "pagoda", "animation")).toEqual([]);
  });
});
