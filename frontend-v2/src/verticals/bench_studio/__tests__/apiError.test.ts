import { describe, expect, it } from "vitest";
import { apiErrorDetail, apiFieldErrors } from "../apiError";

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

describe("apiFieldErrors (round-4 K1, t2 SOLL-2)", () => {
  it("returns each Pydantic item with its path, type and limit", () => {
    const body = {
      detail: [
        { type: "string_too_long", loc: ["body", "title"], msg: "String should have at most 200 characters",
          input: "x", ctx: { max_length: 200 } },
        { type: "string_too_long", loc: ["body", "models", 1, "display_tag"], msg: "…", ctx: { max_length: 80 } },
      ],
    };
    expect(apiFieldErrors(err(422, body))).toEqual([
      { path: ["title"], type: "string_too_long", msg: "String should have at most 200 characters", ctx: { max_length: 200 } },
      { path: ["models", 1, "display_tag"], type: "string_too_long", msg: "…", ctx: { max_length: 80 } },
    ]);
  });

  it("is empty for everything that is not a validation list", () => {
    expect(apiFieldErrors(err(422, { detail: "Spark offline" }))).toEqual([]);
    expect(apiFieldErrors(err(500, "<html>"))).toEqual([]);
    expect(apiFieldErrors(new Error("network down"))).toEqual([]);
    expect(apiFieldErrors(err(422, { detail: [{ msg: "no loc" }] }))).toEqual([]);
  });
});
