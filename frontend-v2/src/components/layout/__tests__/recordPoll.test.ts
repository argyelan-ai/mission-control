import { describe, expect, it } from "vitest";
import { recordPollMs } from "@/components/layout/useChatList";

// User test 21.11.2026: while CHAT_RECORD_ENABLED is off the list now
// answers 200 `{chats: [], enabled: false}` instead of a 404 (no red console
// line). The poll must stop on that answer just as it did on the 404.
describe("recordPollMs — record off", () => {
  it("stops polling when the backend says the record is off", () => {
    expect(recordPollMs(null, 10_000, { chats: [], enabled: false })).toBe(false);
  });

  it("keeps polling while the record is on", () => {
    expect(recordPollMs(null, 10_000, { chats: [], enabled: true })).toBe(10_000);
  });

  it("an older backend without the field keeps today's behaviour", () => {
    expect(recordPollMs(null, 60_000, { chats: [] })).toBe(60_000);
    expect(recordPollMs(new Error("API 404: {}"), 60_000, undefined)).toBe(false);
  });
});
