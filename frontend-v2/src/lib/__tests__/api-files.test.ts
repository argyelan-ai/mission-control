import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { api } from "@/lib/api";

// Files API — URL-builder contract tests. The API client's URL shape is the
// contract under test (mirrors api-knowledge-scope.test.ts), so we spy on
// fetch and assert the path + query string without any render dependency.

describe("api.files — URL building", () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    const storage = {
      getItem: () => null,
      setItem: () => undefined,
      removeItem: () => undefined,
      clear: () => undefined,
    };
    Object.defineProperty(globalThis, "localStorage", {
      value: storage,
      configurable: true,
      writable: true,
    });
    fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("{}", {
        status: 200,
        headers: { "Content-Type": "application/json" },
      })
    );
  });

  afterEach(() => {
    fetchSpy.mockRestore();
  });

  function calledUrl(): string {
    return String(fetchSpy.mock.calls[0]?.[0] ?? "");
  }

  it("list(root, subpath) builds /files/list?root=&subpath=", async () => {
    await api.files.list("vault", "sub");
    const url = calledUrl();
    expect(url).toContain("/api/v1/files/list?");
    expect(url).toContain("root=vault");
    expect(url).toContain("subpath=sub");
  });

  it("list(root) omits subpath when not given", async () => {
    await api.files.list("deliverables");
    const url = calledUrl();
    expect(url).toContain("root=deliverables");
    expect(url).not.toContain("subpath=");
  });

  it("roots() hits /files/roots", async () => {
    await api.files.roots();
    expect(calledUrl()).toContain("/api/v1/files/roots");
  });

  it("meta(root, subpath) builds /files/meta with both params", async () => {
    await api.files.meta("workspaces", "proj/README.md");
    const url = calledUrl();
    expect(url).toContain("/api/v1/files/meta?");
    expect(url).toContain("root=workspaces");
    // value is URL-encoded
    expect(url).toContain("subpath=proj%2FREADME.md");
  });

  it("search(params) forwards q + filters", async () => {
    await api.files.search({ q: "report", type: "file", root: "vault", limit: 50 });
    const url = calledUrl();
    expect(url).toContain("/api/v1/files/search?");
    expect(url).toContain("q=report");
    expect(url).toContain("type=file");
    expect(url).toContain("root=vault");
    expect(url).toContain("limit=50");
  });

  it("contentUrl returns a BASE-prefixed absolute URL, with download flag", () => {
    const url = api.files.contentUrl("vault", "a/b.png");
    expect(url).toContain("/api/v1/files/content?");
    expect(url).toContain("root=vault");
    expect(url).toContain("subpath=a%2Fb.png");
    expect(url).not.toContain("download=");

    const dl = api.files.contentUrl("vault", "a/b.png", true);
    expect(dl).toContain("download=true");
  });

  it("open() POSTs body with reveal flag", async () => {
    await api.files.open("deliverables", "out.pdf", true);
    const url = calledUrl();
    const init = fetchSpy.mock.calls[0]?.[1] as RequestInit;
    expect(url).toContain("/api/v1/files/open");
    expect(init.method).toBe("POST");
    expect(String(init.body)).toContain('"reveal":true');
    expect(String(init.body)).toContain('"root":"deliverables"');
  });

  it("delete(root, subpaths) POSTs /api/v1/files/delete with JSON body", async () => {
    await api.files.delete("vault", ["a.pdf", "b/c.txt"]);
    const url = calledUrl();
    const init = fetchSpy.mock.calls[0]?.[1] as RequestInit;
    expect(url).toContain("/api/v1/files/delete");
    expect(init.method).toBe("POST");
    expect(String(init.body)).toContain('"root":"vault"');
    expect(String(init.body)).toContain('"subpaths":["a.pdf","b/c.txt"]');
  });

  it("trash.list() GETs /api/v1/files/trash with no body", async () => {
    await api.files.trash.list();
    const url = calledUrl();
    const init = fetchSpy.mock.calls[0]?.[1] as RequestInit;
    expect(url).toContain("/api/v1/files/trash");
    // GET — default method, no body.
    expect(init?.method ?? "GET").toBe("GET");
    expect(init?.body).toBeUndefined();
  });

  it("trash.restore([ids]) POSTs /api/v1/files/trash/restore with a trash_ids body", async () => {
    await api.files.trash.restore(["20260101-120000/deliverables/a.pdf"]);
    const url = calledUrl();
    const init = fetchSpy.mock.calls[0]?.[1] as RequestInit;
    expect(url).toContain("/api/v1/files/trash/restore");
    expect(init.method).toBe("POST");
    expect(String(init.body)).toContain('"trash_ids":["20260101-120000/deliverables/a.pdf"]');
  });

  it("trash.purge([ids]) POSTs /api/v1/files/trash/purge with a trash_ids body", async () => {
    await api.files.trash.purge(["20260101-120000/deliverables/a.pdf"]);
    const url = calledUrl();
    const init = fetchSpy.mock.calls[0]?.[1] as RequestInit;
    expect(url).toContain("/api/v1/files/trash/purge");
    expect(init.method).toBe("POST");
    expect(String(init.body)).toContain('"trash_ids":["20260101-120000/deliverables/a.pdf"]');
  });
});
