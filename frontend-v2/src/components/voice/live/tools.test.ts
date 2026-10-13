/** Live voice — browser side of the tools (voice PR 2): pure parts. */
import { describe, expect, it } from "vitest";
import { UtteranceLog, functionCallFromEvent, functionOutputEvent, toolFailureOutput } from "./tools";
import { VOICE_PREFIX, parseVoiceMessage } from "./voiceMessage";

describe("functionCallFromEvent", () => {
  it("reads both OpenAI event shapes", () => {
    expect(
      functionCallFromEvent({
        type: "response.output_item.done",
        item: { type: "function_call", call_id: "c1", name: "send_to_agent", arguments: '{"text":"Hi"}' },
      }),
    ).toEqual({ callId: "c1", name: "send_to_agent", args: { text: "Hi" } });
    expect(
      functionCallFromEvent({ type: "response.function_call_arguments.done", call_id: "c2", name: "get_agent_status", arguments: "" }),
    ).toEqual({ callId: "c2", name: "get_agent_status", args: {} });
  });

  it("ignores unknown tools, messages and broken arguments safely", () => {
    expect(functionCallFromEvent({ type: "response.output_item.done", item: { type: "function_call", call_id: "c", name: "press_keys" } })).toBeNull();
    // voice PR 4: stop_agent is a known tool now
    expect(
      functionCallFromEvent({ type: "response.output_item.done", item: { type: "function_call", call_id: "s", name: "stop_agent", arguments: "{}" } }),
    ).toEqual({ callId: "s", name: "stop_agent", args: {} });
    expect(functionCallFromEvent({ type: "response.output_item.done", item: { type: "message" } })).toBeNull();
    expect(
      functionCallFromEvent({ type: "response.output_item.done", item: { type: "function_call", call_id: "c", name: "send_to_agent", arguments: "{oops" } }),
    ).toEqual({ callId: "c", name: "send_to_agent", args: {} });
  });
});

describe("functionOutputEvent", () => {
  it("wraps the answer as a function_call_output item", () => {
    expect(functionOutputEvent("c1", { delivered: true })).toEqual({
      type: "conversation.item.create",
      item: { type: "function_call_output", call_id: "c1", output: '{"delivered":true}' },
    });
  });
});

describe("UtteranceLog", () => {
  it("is pending from commit until the transcription is final", () => {
    const log = new UtteranceLog();
    log.committed("u1");
    expect(log.pending()).toBe(true);
    log.delta("u1", "hal");
    expect(log.pending()).toBe(true);
    log.final("u1", " hallo ");
    expect(log.pending()).toBe(false);
    expect(log.take()).toEqual({ ids: ["u1"], text: "hallo" });
  });

  it("joins every unsent utterance in order and forgets delivered ones", () => {
    const log = new UtteranceLog();
    log.final("u1", "erstens");
    log.final("u2", "zweitens");
    expect(log.take().text).toBe("erstens zweitens");
    log.consume(["u1", "u2"]);
    log.final("u3", "drittens");
    expect(log.take()).toEqual({ ids: ["u3"], text: "drittens" });
  });

  it("last(): the newest finished, unsent utterance on its own (the stop check)", () => {
    const log = new UtteranceLog();
    expect(log.last()).toBeNull();
    log.final("u1", "mach erst X");
    log.final("u2", " Stopp! ");
    log.committed("u3"); // still transcribing: not the last FINISHED one
    expect(log.last()).toEqual({ id: "u2", text: "Stopp!" });
    log.consume(["u2"]);
    expect(log.last()).toEqual({ id: "u1", text: "mach erst X" });
    log.failed("u3");
    expect(log.last()).toEqual({ id: "u1", text: "mach erst X" });
  });

  it("a failed transcription is not waited for and adds nothing", () => {
    const log = new UtteranceLog();
    log.committed("u1");
    log.failed("u1");
    expect(log.pending()).toBe(false);
    expect(log.take()).toEqual({ ids: [], text: "" });
  });
});

describe("toolFailureOutput", () => {
  it("maps MC refusals to an honest NOT delivered", () => {
    const out = toolFailureOutput("send_to_agent", new Error('API 429: {"detail":{"reason":"rate_limited"}}'));
    expect(out).toMatchObject({ delivered: false, reason: "rate_limited" });
    expect(String(out.say)).toMatch(/^NOT delivered/);
    expect(toolFailureOutput("send_to_agent", new TypeError("Failed to fetch"))).toMatchObject({
      delivered: false,
      reason: "network",
    });
  });

  it("a failed stop is NOT stopped and points to the Stop button", () => {
    const out = toolFailureOutput("stop_agent", new Error('API 403: {"detail":{"reason":"call_unknown"}}'));
    expect(out).toMatchObject({ stopped: false, reason: "call_unknown" });
    expect(String(out.say)).toMatch(/^NOT stopped/);
    expect(String(out.say)).toMatch(/Stop button/);
  });

  it("a failed status read says unknown, never a guess", () => {
    expect(toolFailureOutput("get_agent_status", new Error("API 500: x"))).toMatchObject({ state: "unknown" });
  });
});

describe("parseVoiceMessage", () => {
  const NOTE = "(Voice transcript: may contain mis-hearings.)";

  it("splits wording, verbatim words and the agent note", () => {
    expect(parseVoiceMessage(`${VOICE_PREFIX} Mach Y\nWortlaut: „mach mal Y“\n${NOTE}`)).toEqual({
      text: "Mach Y",
      verbatim: "mach mal Y",
    });
    expect(parseVoiceMessage(`${VOICE_PREFIX} Mach Y\n${NOTE}`)).toEqual({ text: "Mach Y", verbatim: null });
  });

  it("only a message that opens with the marker is a voice message", () => {
    expect(parseVoiceMessage("Mach Y")).toBeNull();
    expect(parseVoiceMessage(`bitte ${VOICE_PREFIX} Mach Y`)).toBeNull();
    expect(parseVoiceMessage("[per Stimme]x")).toBeNull();
  });
});
