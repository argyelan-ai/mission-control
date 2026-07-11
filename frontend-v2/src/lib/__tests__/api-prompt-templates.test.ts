import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { api } from "../api";

// BASE_URL is "" in vitest (NEXT_PUBLIC_API_URL unset), so fetch URLs are
// the bare /api/v1 paths. localStorage stub pattern from FilePreview.test.tsx.

describe("api.promptTemplates", () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    Object.defineProperty(globalThis, "localStorage", {
      value: {
        getItem: () => "tok",
        setItem: () => undefined,
        removeItem: () => undefined,
        clear: () => undefined,
      },
      configurable: true,
      writable: true,
    });
    fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify([]), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
  });

  afterEach(() => {
    fetchSpy?.mockRestore();
  });

  it("list() GETs /api/v1/prompt-templates with q + tag params", async () => {
    await api.promptTemplates.list({ q: "cube", tag: "3d" });
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(String(url)).toBe("/api/v1/prompt-templates?q=cube&tag=3d");
    expect(init?.method).toBeUndefined(); // default GET
    expect((init?.headers as Record<string, string>).Authorization).toBe("Bearer tok");
  });

  it("list() without params has no query string", async () => {
    await api.promptTemplates.list();
    const [url] = fetchSpy.mock.calls[0] as [string];
    expect(String(url)).toBe("/api/v1/prompt-templates");
  });

  it("create() POSTs the JSON body", async () => {
    fetchSpy.mockResolvedValue(
      new Response(JSON.stringify({ id: "t1" }), {
        status: 201,
        headers: { "Content-Type": "application/json" },
      }),
    );
    await api.promptTemplates.create({ title: "Cube", body: "Spin it", tags: ["3d"] });
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(String(url)).toBe("/api/v1/prompt-templates");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ title: "Cube", body: "Spin it", tags: ["3d"] });
  });

  it("update() PATCHes partial data by id", async () => {
    fetchSpy.mockResolvedValue(
      new Response(JSON.stringify({ id: "t1" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    await api.promptTemplates.update("t1", { tags: ["web"] });
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(String(url)).toBe("/api/v1/prompt-templates/t1");
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(String(init.body))).toEqual({ tags: ["web"] });
  });

  it("remove() DELETEs by id and resolves on 204", async () => {
    fetchSpy.mockResolvedValue(new Response(null, { status: 204 }));
    await expect(api.promptTemplates.remove("t1")).resolves.toBeUndefined();
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(String(url)).toBe("/api/v1/prompt-templates/t1");
    expect(init.method).toBe("DELETE");
  });
});
