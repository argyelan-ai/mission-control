import { describe, expect, it } from "vitest";
import { apiErrorDetail } from "../apiError";

const err = (status: number, body: unknown) =>
  new Error(`API ${status}: ${typeof body === "string" ? body : JSON.stringify(body)}`);

describe("apiErrorDetail", () => {
  it("returns a plain-text detail as is", () => {
    expect(apiErrorDetail(err(422, { detail: "Spark offline" }), "fallback")).toBe("Spark offline");
  });

  it("reads a Pydantic validation list: field name + message (round-2 t2 MUSS-1)", () => {
    const body = {
      detail: [
        {
          type: "string_too_short",
          loc: ["body", "models", 0, "label"],
          msg: "String should have at least 1 character",
          input: "",
        },
      ],
    };
    expect(apiErrorDetail(err(422, body), "fallback")).toBe(
      "label: String should have at least 1 character",
    );
  });

  it("says how many more problems there are", () => {
    const body = {
      detail: [
        { loc: ["body", "title"], msg: "Field required" },
        { loc: ["body", "models"], msg: "List should have at least 1 item" },
      ],
    };
    expect(apiErrorDetail(err(422, body), "fallback")).toBe("title: Field required (+1)");
  });

  it("shows the message alone when the location has no field name", () => {
    expect(apiErrorDetail(err(422, { detail: [{ loc: ["body"], msg: "Invalid JSON" }] }), "fb")).toBe(
      "Invalid JSON",
    );
  });

  it("falls back for anything it cannot read", () => {
    expect(apiErrorDetail(new Error("network down"), "fallback")).toBe("fallback");
    expect(apiErrorDetail(err(500, "<html>"), "fallback")).toBe("fallback");
    expect(apiErrorDetail(err(422, { detail: [] }), "fallback")).toBe("fallback");
    expect(apiErrorDetail(err(422, { detail: [{ loc: ["body"] }] }), "fallback")).toBe("fallback");
    expect(apiErrorDetail(err(422, { detail: "" }), "fallback")).toBe("fallback");
    expect(apiErrorDetail("not an error", "fallback")).toBe("fallback");
  });
});
