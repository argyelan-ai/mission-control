import { describe, it, expect } from "vitest";

// In the vmForks pool the test's global object is jsdom's window, and jsdom
// has none of the web-stream classes that every real browser has. Without
// the polyfill in test-setup.ts a test that builds a stream by hand (a mocked
// SSE or chat body) passes in the forks pool and fails only in the vm pool.
const STREAM_GLOBALS = [
  "ReadableStream",
  "WritableStream",
  "TransformStream",
  "TextDecoderStream",
  "TextEncoderStream",
] as const;

describe("web-stream globals exist in every test pool", () => {
  it.each(STREAM_GLOBALS)("%s is a constructor on globalThis", (name) => {
    expect(typeof (globalThis as Record<string, unknown>)[name]).toBe("function");
  });

  it("a Response body is an instance of the global ReadableStream", () => {
    const stream = new ReadableStream<Uint8Array>({
      start(c) {
        c.enqueue(new TextEncoder().encode("hi"));
        c.close();
      },
    });
    const res = new Response(stream);
    expect(res.body).toBeInstanceOf(globalThis.ReadableStream);
  });

  it("a hand-built stream pipes through TextDecoderStream", async () => {
    const stream = new ReadableStream<Uint8Array>({
      start(c) {
        c.enqueue(new TextEncoder().encode("data: ok\n\n"));
        c.close();
      },
    });
    const reader = stream.pipeThrough(new TextDecoderStream()).getReader();
    let text = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      text += value;
    }
    expect(text).toBe("data: ok\n\n");
  });
});
