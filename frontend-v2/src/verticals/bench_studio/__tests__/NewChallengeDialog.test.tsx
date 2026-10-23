import { describe, it, expect, vi, beforeEach, onTestFinished } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { PromptTemplate } from "../types";

vi.mock("@/verticals/bench_studio/api", () => ({
  benchApi: {
    sparkModels: {
      get: vi.fn().mockResolvedValue({ reachable: true, models: [], active: null }),
    },
    effortLevels: {
      get: vi.fn().mockResolvedValue({
        served_model_id: "deepseek-v4", reachable: true, status: "ok",
        thinking_key: "reasoning_effort", effort_param: "reasoning_effort",
        levels: ["off", "low", "medium", "high", "max"], default_observed: "on",
        probed_at: null, serving_since: null,
      }),
      check: vi.fn(),
    },
    challenges: {
      list: vi.fn(),
      get: vi.fn(),
      create: vi.fn().mockResolvedValue({ id: "ch-new", title: "Test" }),
      draft: vi.fn(),
      rerender: vi.fn(),
    },
    entries: { retry: vi.fn() },
    promptTemplates: {
      list: vi.fn().mockResolvedValue([
        { id: "tpl-1", title: "Bouncing Balls", body: "Animate 100 bouncing balls", tags: [], created_at: "", updated_at: "" },
        { id: "tpl-2", title: "Sorting Race", body: "Visualize sorting algorithms", tags: [], created_at: "", updated_at: "" },
      ]),
    },
    sharedSubpath: (p: string) => p.replace(/^\/shared-deliverables\//, ""),
  },
}));

vi.mock("@/lib/api", () => ({
  api: {
    agents: { list: vi.fn().mockResolvedValue([]) },
  },
}));

vi.mock("@/lib/notify", () => ({
  notify: { success: vi.fn(), error: vi.fn() },
}));

import { benchApi } from "@/verticals/bench_studio/api";
import { api } from "@/lib/api";
import { NewChallengeDialog } from "../NewChallengeDialog";
import { EffortChips } from "../EffortChips";

function renderDialog(props?: { prefillTemplate?: PromptTemplate | null }) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <NewChallengeDialog
        open
        onClose={() => {}}
        prefillTemplate={props?.prefillTemplate ?? null}
      />
    </QueryClientProvider>
  );
}

describe("NewChallengeDialog — template picker", () => {
  beforeEach(() => {
    vi.mocked(benchApi.promptTemplates.list).mockResolvedValue([
      { id: "tpl-1", title: "Bouncing Balls", body: "Animate 100 bouncing balls", tags: [], created_at: "", updated_at: "" },
      { id: "tpl-2", title: "Sorting Race", body: "Visualize sorting algorithms", tags: [], created_at: "", updated_at: "" },
    ]);
  });

  it("renders a template selector with Freitext default + one option per template", async () => {
    renderDialog();
    const select = await screen.findByRole("combobox", { name: /template/i });
    expect(select).toBeTruthy();
    // "Freitext" option should be present (default)
    expect(screen.getByRole("option", { name: /free text/i })).toBeTruthy();
    // Template options
    expect(await screen.findByRole("option", { name: /bouncing balls/i })).toBeTruthy();
    expect(screen.getByRole("option", { name: /sorting race/i })).toBeTruthy();
  });

  it("selecting a template fills the prompt textarea with the template body", async () => {
    renderDialog();
    // Wait for template options to load
    await screen.findByRole("option", { name: /bouncing balls/i });
    const select = screen.getByRole("combobox", { name: /template/i });
    await userEvent.selectOptions(select, "tpl-1");
    const textarea = screen.getByPlaceholderText(/prompt/i);
    expect((textarea as HTMLTextAreaElement).value).toBe("Animate 100 bouncing balls");
  });

  it("create body contains prompt_template_id when a template is selected", async () => {
    vi.mocked(benchApi.challenges.create).mockResolvedValue({
      id: "ch-new", title: "Test", prompt_template_id: "tpl-1",
      prompt_text: "Animate 100 bouncing balls", mode: "side_by_side",
      status: "generating", series_label: null, series_no: null, record_duration_s: null,
      composed_video_path: null, content_pipeline_id: null, error: null, archived_at: null,
      created_at: "", updated_at: "", entries: [],
    });

    renderDialog();

    // Wait for template options to load, then select
    await screen.findByRole("option", { name: /bouncing balls/i });
    const select = screen.getByRole("combobox", { name: /template/i });
    await userEvent.selectOptions(select, "tpl-1");

    // Fill title
    const titleInput = screen.getByPlaceholderText(/title/i);
    await userEvent.type(titleInput, "My Test");

    // Fill model label (required for valid form)
    const modelLabelInput = screen.getByPlaceholderText(/Label \(e\.g\./i);
    await userEvent.type(modelLabelInput, "DeepSeek");

    // Submit
    const submitBtn = screen.getByRole("button", { name: /Start Challenge/i });
    await userEvent.click(submitBtn);

    await waitFor(() => {
      expect(benchApi.challenges.create).toHaveBeenCalledWith(
        expect.objectContaining({ prompt_template_id: "tpl-1" })
      );
    });
  });

  it("switching back to Freitext clears prompt_template_id in the create body", async () => {
    vi.mocked(benchApi.challenges.create).mockResolvedValue({
      id: "ch-new", title: "Test", prompt_template_id: null,
      prompt_text: "Custom text", mode: "side_by_side",
      status: "generating", series_label: null, series_no: null, record_duration_s: null,
      composed_video_path: null, content_pipeline_id: null, error: null, archived_at: null,
      created_at: "", updated_at: "", entries: [],
    });

    renderDialog();

    // Wait for options to load, then select template
    await screen.findByRole("option", { name: /bouncing balls/i });
    const select = screen.getByRole("combobox", { name: /template/i });
    await userEvent.selectOptions(select, "tpl-1");

    // Switch back to Freitext
    await userEvent.selectOptions(select, "");

    // Textarea should still have template text (editable), but template id cleared
    // Type custom text
    const textarea = screen.getByPlaceholderText(/prompt/i);
    await userEvent.clear(textarea);
    await userEvent.type(textarea, "Custom text");

    // Fill title and model label
    const titleInput = screen.getByPlaceholderText(/title/i);
    await userEvent.type(titleInput, "My Test");
    const modelLabelInput = screen.getByPlaceholderText(/Label \(e\.g\./i);
    await userEvent.type(modelLabelInput, "DeepSeek");

    const submitBtn = screen.getByRole("button", { name: /Start Challenge/i });
    await userEvent.click(submitBtn);

    await waitFor(() => {
      expect(benchApi.challenges.create).toHaveBeenCalledWith(
        expect.objectContaining({ prompt_template_id: null })
      );
    });
  });

  it("sends display_tag per model (typed value; null when left blank)", async () => {
    renderDialog();
    await screen.findByRole("option", { name: /bouncing balls/i });

    // Fill mandatory fields (Freitext path)
    await userEvent.type(screen.getByPlaceholderText(/title/i), "Tag Test");
    const textarea = screen.getByPlaceholderText(/prompt/i);
    await userEvent.type(textarea, "Some prompt");
    await userEvent.type(screen.getByPlaceholderText(/Label \(e\.g\./i), "Qwen");

    // Tag input names the derived default as placeholder (spark row), in
    // the UI language (round-4 r2 N7 — it read "LOCAL · DIRECT API").
    const tagInput = screen.getByRole("textbox", { name: /tag 1/i });
    expect((tagInput as HTMLInputElement).placeholder).toContain("local · direct API");

    // Type a custom tag
    await userEvent.type(tagInput, "OMP · DGX SPARK");

    await userEvent.click(screen.getByRole("button", { name: /Start Challenge/i }));

    await waitFor(() => {
      expect(benchApi.challenges.create).toHaveBeenCalledWith(
        expect.objectContaining({
          models: [expect.objectContaining({ label: "Qwen", display_tag: "OMP · DGX SPARK" })],
        })
      );
    });
  });

  it("sends display_tag: null when the tag field is left empty", async () => {
    renderDialog();
    await screen.findByRole("option", { name: /bouncing balls/i });

    await userEvent.type(screen.getByPlaceholderText(/title/i), "Tag Test");
    await userEvent.type(screen.getByPlaceholderText(/prompt/i), "Some prompt");
    await userEvent.type(screen.getByPlaceholderText(/Label \(e\.g\./i), "Qwen");

    await userEvent.click(screen.getByRole("button", { name: /Start Challenge/i }));

    await waitFor(() => {
      expect(benchApi.challenges.create).toHaveBeenCalledWith(
        expect.objectContaining({
          models: [expect.objectContaining({ display_tag: null })],
        })
      );
    });
  });

  it("prefillTemplate prop preselects the template in the dropdown", async () => {
    const prefill: PromptTemplate = {
      id: "tpl-2",
      title: "Sorting Race",
      body: "Visualize sorting algorithms",
      tags: [],
      created_at: "",
      updated_at: "",
    };
    renderDialog({ prefillTemplate: prefill });

    const select = await screen.findByRole("combobox", { name: /template/i });
    await waitFor(() => {
      expect((select as HTMLSelectElement).value).toBe("tpl-2");
    });
  });

  it("when template is selected and edited, create body includes edited prompt_text AND template_id", async () => {
    const editedText = "My custom edited version of the prompt";
    vi.mocked(benchApi.challenges.create).mockResolvedValue({
      id: "ch-new", title: "Test", prompt_template_id: "tpl-1",
      prompt_text: editedText, mode: "side_by_side",
      status: "generating", series_label: null, series_no: null, record_duration_s: null,
      composed_video_path: null, content_pipeline_id: null, error: null, archived_at: null,
      created_at: "", updated_at: "", entries: [],
    });
    vi.mocked(benchApi.sparkModels.get).mockResolvedValueOnce({
      reachable: true, models: ["deepseek-v4"], active: "deepseek-v4",
    });

    renderDialog();

    // Wait for templates to load
    await screen.findByRole("option", { name: /bouncing balls/i });

    // Select template "Bouncing Balls"
    const select = screen.getByRole("combobox", { name: /template/i });
    await userEvent.selectOptions(select, "tpl-1");

    // Verify textarea is filled with template body
    const textarea = screen.getByPlaceholderText(/prompt/i);
    await waitFor(() => {
      expect((textarea as HTMLTextAreaElement).value).toBe("Animate 100 bouncing balls");
    });

    // User edits the textarea
    await userEvent.clear(textarea);
    await userEvent.type(textarea, editedText);

    // Fill mandatory fields
    await userEvent.type(screen.getByPlaceholderText(/title/i), "My Test");

    // Fill first model — spark model via the select (Bench #21: fed by
    // benchApi.sparkModels.get). aria-label, not placeholder: the auto row's
    // placeholder previews the live active model and isn't a fixed string.
    const labelInput = screen.getByRole("textbox", { name: /label 1/i });
    await userEvent.type(labelInput, "DeepSeek");
    const sparkSelect = await screen.findByRole("combobox", { name: /vLLM model 1/i });
    await userEvent.selectOptions(sparkSelect, "deepseek-v4");

    // Now submit button should be enabled
    const submitBtn = screen.getByRole("button", { name: /Start Challenge/i });
    await waitFor(() => {
      expect(submitBtn).not.toBeDisabled();
    });

    await userEvent.click(submitBtn);

    // Verify the mutation was called with edited text AND template id
    await waitFor(() => {
      expect(benchApi.challenges.create).toHaveBeenCalledWith(
        expect.objectContaining({
          prompt_template_id: "tpl-1",
          prompt_text: editedText,
        })
      );
    });
  });
});

describe("NewChallengeDialog — record_duration_s (Bench #18 video length)", () => {
  it("defaults the video length field to 20s and submits it", async () => {
    renderDialog();
    await screen.findByRole("option", { name: /bouncing balls/i });

    const durationInput = screen.getByLabelText(/video length/i) as HTMLInputElement;
    expect(durationInput.value).toBe("20");

    await userEvent.type(screen.getByPlaceholderText(/title/i), "Duration Test");
    await userEvent.type(screen.getByPlaceholderText(/prompt/i), "Some prompt");
    await userEvent.type(screen.getByPlaceholderText(/Label \(e\.g\./i), "Qwen");

    await userEvent.click(screen.getByRole("button", { name: /Start Challenge/i }));

    await waitFor(() => {
      expect(benchApi.challenges.create).toHaveBeenCalledWith(
        expect.objectContaining({ record_duration_s: 20 })
      );
    });
  });

  it("sends a custom video length value", async () => {
    renderDialog();
    await screen.findByRole("option", { name: /bouncing balls/i });

    const durationInput = screen.getByLabelText(/video length/i) as HTMLInputElement;
    await userEvent.clear(durationInput);
    await userEvent.type(durationInput, "45");

    await userEvent.type(screen.getByPlaceholderText(/title/i), "Duration Test 2");
    await userEvent.type(screen.getByPlaceholderText(/prompt/i), "Some prompt");
    await userEvent.type(screen.getByPlaceholderText(/Label \(e\.g\./i), "Qwen");

    await userEvent.click(screen.getByRole("button", { name: /Start Challenge/i }));

    await waitFor(() => {
      expect(benchApi.challenges.create).toHaveBeenCalledWith(
        expect.objectContaining({ record_duration_s: 45 })
      );
    });
  });

  it("clears the field to empty and still submits the 20s default (not a raw empty value)", async () => {
    renderDialog();
    await screen.findByRole("option", { name: /bouncing balls/i });

    const durationInput = screen.getByLabelText(/video length/i) as HTMLInputElement;
    await userEvent.clear(durationInput);
    expect(durationInput.value).toBe("");

    await userEvent.type(screen.getByPlaceholderText(/title/i), "Duration Test 3");
    await userEvent.type(screen.getByPlaceholderText(/prompt/i), "Some prompt");
    await userEvent.type(screen.getByPlaceholderText(/Label \(e\.g\./i), "Qwen");

    await userEvent.click(screen.getByRole("button", { name: /Start Challenge/i }));

    await waitFor(() => {
      expect(benchApi.challenges.create).toHaveBeenCalledWith(
        expect.objectContaining({ record_duration_s: 20 })
      );
    });
  });

  it("blurring the empty field re-normalizes the displayed value to 20", async () => {
    renderDialog();
    await screen.findByRole("option", { name: /bouncing balls/i });

    const durationInput = screen.getByLabelText(/video length/i) as HTMLInputElement;
    await userEvent.clear(durationInput);
    await userEvent.tab(); // blur

    expect(durationInput.value).toBe("20");
  });

  it("clamps an out-of-range typed value (e.g. 200) down to the 60s max on blur", async () => {
    renderDialog();
    await screen.findByRole("option", { name: /bouncing balls/i });

    const durationInput = screen.getByLabelText(/video length/i) as HTMLInputElement;
    await userEvent.clear(durationInput);
    await userEvent.type(durationInput, "200");
    await userEvent.tab(); // blur

    expect(durationInput.value).toBe("60");
  });

  it("typing a multi-digit value below 10 is not corrupted by keystroke-level clamping", async () => {
    // Regression guard: an earlier implementation clamped on every
    // keystroke, so typing "10" digit-by-digit produced "50" (the
    // intermediate "1" got clamped to the 5 minimum, then the next "0"
    // was appended onto THAT). The raw-string + blur-normalize approach
    // must not reintroduce this.
    renderDialog();
    await screen.findByRole("option", { name: /bouncing balls/i });

    const durationInput = screen.getByLabelText(/video length/i) as HTMLInputElement;
    await userEvent.clear(durationInput);
    await userEvent.type(durationInput, "10");

    expect(durationInput.value).toBe("10");
  });
});

describe("NewChallengeDialog — label autofill", () => {
  const sparkyAgent = {
    id: "agent-1",
    name: "Sparky",
    model: "Qwen/Qwen3.6-35B-A3B-FP8",
    harness: "omp",
  } as never;

  beforeEach(() => {
    vi.mocked(api.agents.list).mockResolvedValue([sparkyAgent] as never);
  });

  it("selecting a spark model mirrors it into the label while untouched", async () => {
    vi.mocked(benchApi.sparkModels.get).mockResolvedValueOnce({
      reachable: true, models: ["deepseek-v4"], active: "deepseek-v4",
    });
    renderDialog();
    await screen.findByRole("option", { name: /bouncing balls/i });

    const sparkSelect = await screen.findByRole("combobox", { name: /vLLM model 1/i });
    await userEvent.selectOptions(sparkSelect, "deepseek-v4");

    const labelInput = screen.getByRole("textbox", { name: /label 1/i });
    expect((labelInput as HTMLInputElement).value).toBe("deepseek-v4");
  });

  it("choosing 'Active model (auto)' leaves the label empty, showing the active model only as a placeholder (review MINOR 1)", async () => {
    vi.mocked(benchApi.sparkModels.get).mockResolvedValueOnce({
      reachable: true, models: ["deepseek-v4", "qwen3.6"], active: "qwen3.6",
    });
    renderDialog();
    await screen.findByRole("option", { name: /bouncing balls/i });

    const sparkSelect = await screen.findByRole("combobox", { name: /vLLM model 1/i });
    // Pick a concrete model first (mirrors into the label), then switch
    // back to auto — the label must be CLEARED, not stuck on the previous
    // selection, and the active model must show only as a placeholder
    // (never written into the submitted value — see the "submits an empty
    // label" test below).
    await userEvent.selectOptions(sparkSelect, "deepseek-v4");
    const labelInput = screen.getByRole("textbox", { name: /label 1/i }) as HTMLInputElement;
    expect(labelInput.value).toBe("deepseek-v4");

    await userEvent.selectOptions(sparkSelect, "");
    expect(labelInput.value).toBe("");
    expect(labelInput.placeholder).toContain("qwen3.6");
  });

  it("a hand-edited label on an auto row is still submitted as-is", async () => {
    vi.mocked(benchApi.sparkModels.get).mockResolvedValueOnce({
      reachable: true, models: ["qwen3.6"], active: "qwen3.6",
    });
    renderDialog();
    await screen.findByRole("option", { name: /bouncing balls/i });
    await screen.findByRole("combobox", { name: /vLLM model 1/i }); // wait for the spark-models query

    const labelInput = screen.getByRole("textbox", { name: /label 1/i });
    await userEvent.type(labelInput, "Mein Auto-Label");

    await userEvent.type(screen.getByPlaceholderText(/title/i), "Hand-Edited Label Test");
    await userEvent.type(screen.getByPlaceholderText(/prompt/i), "Some prompt");
    await userEvent.click(screen.getByRole("button", { name: /Start Challenge/i }));

    await waitFor(() => {
      expect(benchApi.challenges.create).toHaveBeenCalledWith(
        expect.objectContaining({
          models: [expect.objectContaining({ spark_model: "", label: "Mein Auto-Label" })],
        })
      );
    });
  });

  it("selecting an agent fills the label with the agent's model", async () => {
    renderDialog();
    await screen.findByRole("option", { name: /bouncing balls/i });

    // Switch row to agent source
    await userEvent.selectOptions(screen.getByRole("combobox", { name: /source 1/i }), "agent");
    await userEvent.selectOptions(await screen.findByRole("combobox", { name: /agent 1/i }), "agent-1");

    const labelInput = screen.getByRole("textbox", { name: /label 1/i });
    expect((labelInput as HTMLInputElement).value).toBe("Qwen/Qwen3.6-35B-A3B-FP8");
  });

  it("a hand-edited label is not overwritten by later selection", async () => {
    renderDialog();
    await screen.findByRole("option", { name: /bouncing balls/i });

    const labelInput = screen.getByRole("textbox", { name: /label 1/i });
    await userEvent.type(labelInput, "Mein Label");

    await userEvent.selectOptions(screen.getByRole("combobox", { name: /source 1/i }), "agent");
    await userEvent.selectOptions(await screen.findByRole("combobox", { name: /agent 1/i }), "agent-1");

    expect((labelInput as HTMLInputElement).value).toBe("Mein Label");
  });
});

describe("NewChallengeDialog — vanilla (direct-API) spark row (Bench #21)", () => {
  it("labels the source option 'Direkt-API' (same word as the entry pill) instead of 'Spark'", async () => {
    renderDialog();
    await screen.findByRole("option", { name: /bouncing balls/i });
    expect(screen.getByRole("option", { name: /^direct api$/i })).toBeTruthy();
    expect(screen.queryByRole("option", { name: /vanilla/i })).toBeNull();
    expect(screen.queryByRole("option", { name: /^spark$/i })).toBeNull();
  });

  it("shows a select fed by benchApi.sparkModels.get with an auto option first", async () => {
    vi.mocked(benchApi.sparkModels.get).mockResolvedValueOnce({
      reachable: true, models: ["deepseek-v4", "qwen3.6"], active: "qwen3.6",
    });
    renderDialog();
    await screen.findByRole("option", { name: /bouncing balls/i });

    const sparkSelect = await screen.findByRole("combobox", { name: /vLLM model 1/i });
    const options = Array.from(sparkSelect.querySelectorAll("option")).map((o) => o.textContent);
    expect(options[0]).toMatch(/active model \(auto\)/i);
    expect(options).toContain("deepseek-v4");
    expect(options).toContain("qwen3.6");
  });

  it("choosing the auto option with an untouched label submits label='' and spark_model='' (backend resolves+fills both at create)", async () => {
    vi.mocked(benchApi.sparkModels.get).mockResolvedValueOnce({
      reachable: true, models: ["qwen3.6"], active: "qwen3.6",
    });
    renderDialog();
    await screen.findByRole("option", { name: /bouncing balls/i });
    await screen.findByRole("combobox", { name: /vLLM model 1/i }); // wait for the spark-models query

    // Auto is the default selection ("") and the label is left untouched —
    // review MINOR 1: this must stay VALID (submit enabled) and must NOT
    // carry the dialog-load-time active model into the submitted label.
    await userEvent.type(screen.getByPlaceholderText(/title/i), "Auto Test");
    await userEvent.type(screen.getByPlaceholderText(/prompt/i), "Some prompt");

    const submitBtn = screen.getByRole("button", { name: /Start Challenge/i });
    expect(submitBtn).not.toBeDisabled();
    await userEvent.click(submitBtn);

    await waitFor(() => {
      expect(benchApi.challenges.create).toHaveBeenCalledWith(
        expect.objectContaining({
          models: [expect.objectContaining({ spark_model: "", label: "" })],
        })
      );
    });
  });

  it("renders a free-text fallback with an offline warning when Spark is unreachable, and blocks submit until a model is typed", async () => {
    vi.mocked(benchApi.sparkModels.get).mockResolvedValueOnce({
      reachable: false, models: [], active: null,
    });
    renderDialog();
    await screen.findByRole("option", { name: /bouncing balls/i });

    await screen.findByText(/spark offline — enter model manually or start later/i);
    const fallbackInput = screen.getByPlaceholderText(/vLLM model \(empty = active\)/);
    expect(fallbackInput.tagName).toBe("INPUT");
    expect(screen.queryByRole("combobox", { name: /vLLM model 1/i })).toBeNull();

    await userEvent.type(screen.getByPlaceholderText(/title/i), "Offline Test");
    await userEvent.type(screen.getByPlaceholderText(/prompt/i), "Some prompt");
    await userEvent.type(screen.getByPlaceholderText(/Label \(e\.g\./), "DeepSeek");

    // NIT 2: still on "auto" (spark_model empty) while confirmed offline —
    // create() would 422 immediately, so submit must stay disabled even
    // though title/prompt/label are all filled in.
    expect(screen.getByRole("button", { name: /Start Challenge/i })).toBeDisabled();

    // Typing a model manually (free-text fallback) is no longer "auto" —
    // submit re-enables.
    await userEvent.type(fallbackInput, "manual-model");
    expect((fallbackInput as HTMLInputElement).value).toBe("manual-model");
    expect(screen.getByRole("button", { name: /Start Challenge/i })).not.toBeDisabled();
  });

  it("treats a spark-models query ERROR the same as unreachable (review NIT 1)", async () => {
    vi.mocked(benchApi.sparkModels.get).mockRejectedValueOnce(new Error("network down"));
    renderDialog();
    await screen.findByRole("option", { name: /bouncing balls/i });

    // Must fall back to the free-text warning UI, not a trap select with
    // only the auto option and no visible warning.
    await screen.findByText(/spark offline/i);
    expect(screen.getByPlaceholderText(/vLLM model \(empty = active\)/).tagName).toBe("INPUT");
    expect(screen.queryByRole("combobox", { name: /vLLM model 1/i })).toBeNull();
  });

  it("surfaces the backend's error detail in the failure toast (review MINOR 2)", async () => {
    const { notify } = await import("@/lib/notify");
    vi.mocked(benchApi.challenges.create).mockRejectedValueOnce(
      new Error('API 422: {"detail":"Spark nicht erreichbar — Modell nicht auflösbar"}')
    );
    renderDialog();
    await screen.findByRole("option", { name: /bouncing balls/i });

    await userEvent.type(screen.getByPlaceholderText(/title/i), "Error Toast Test");
    await userEvent.type(screen.getByPlaceholderText(/prompt/i), "Some prompt");
    await userEvent.type(screen.getByPlaceholderText(/Label \(e\.g\./), "DeepSeek");
    await userEvent.click(screen.getByRole("button", { name: /Start Challenge/i }));

    await waitFor(() => {
      expect(notify.error).toHaveBeenCalledWith("Spark nicht erreichbar — Modell nicht auflösbar");
    });
  });

  it("a validation-list 422 is said at its field, not as a raw toast (round-2 t2 MUSS-1, round-4 SOLL-2)", async () => {
    const { notify } = await import("@/lib/notify");
    vi.mocked(benchApi.challenges.create).mockRejectedValueOnce(
      new Error(
        'API 422: {"detail":[{"type":"string_too_short","loc":["body","models",0,"label"],' +
          '"msg":"String should have at least 1 character","input":""}]}'
      )
    );
    renderDialog();
    await screen.findByRole("option", { name: /bouncing balls/i });

    await userEvent.type(screen.getByPlaceholderText(/title/i), "Validation Toast Test");
    await userEvent.type(screen.getByPlaceholderText(/prompt/i), "Some prompt");
    await userEvent.type(screen.getByPlaceholderText(/Label \(e\.g\./), "DeepSeek");
    await userEvent.click(screen.getByRole("button", { name: /Start Challenge/i }));

    const label = screen.getByRole("textbox", { name: "Label 1" });
    await waitFor(() => expect(label).toHaveAttribute("aria-invalid", "true"));
    expect(screen.getByText("Please fill this in.")).toBeInTheDocument();
    expect(notify.error).not.toHaveBeenCalledWith("label: String should have at least 1 character");
  });

  it("falls back to a generic message when the error body isn't the API detail shape", async () => {
    const { notify } = await import("@/lib/notify");
    vi.mocked(benchApi.challenges.create).mockRejectedValueOnce(new Error("network exploded"));
    renderDialog();
    await screen.findByRole("option", { name: /bouncing balls/i });

    await userEvent.type(screen.getByPlaceholderText(/title/i), "Error Toast Fallback Test");
    await userEvent.type(screen.getByPlaceholderText(/prompt/i), "Some prompt");
    await userEvent.type(screen.getByPlaceholderText(/Label \(e\.g\./), "DeepSeek");
    await userEvent.click(screen.getByRole("button", { name: /Start Challenge/i }));

    await waitFor(() => {
      expect(notify.error).toHaveBeenCalledWith("Challenge could not be started");
    });
  });
});

describe("NewChallengeDialog — effort chip (bench W0.6)", () => {
  async function fillRequired() {
    await screen.findByRole("option", { name: /bouncing balls/i });
    await userEvent.type(screen.getByPlaceholderText(/title/i), "Effort Test");
    await userEvent.type(screen.getByPlaceholderText(/prompt/i), "Some prompt");
    await userEvent.type(screen.getByPlaceholderText(/Label \(e\.g\./i), "DeepSeek");
  }

  it("offers Default plus the levels the probe found, as 44 px radio chips", async () => {
    renderDialog();
    const group = await screen.findByRole("radiogroup", { name: /effort for model 1/i });
    await screen.findByRole("radio", { name: "Max" });
    const names = Array.from(group.querySelectorAll('[role="radio"]')).map((c) => c.textContent);
    expect(names).toEqual(["Default", "Off", "Low", "Medium", "High", "Max"]);
    for (const chip of group.querySelectorAll('[role="radio"]')) {
      expect(chip.className).toContain("min-h-11");
    }
    expect(screen.getByRole("radio", { name: "Default" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByText(/the server thinks by default/i)).toBeInTheDocument();
  });

  it("submits the chosen level for the direct-API row", async () => {
    renderDialog();
    await fillRequired();
    await userEvent.click(await screen.findByRole("radio", { name: "Max" }));
    expect(screen.getByRole("radio", { name: "Max" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByText(/token budget grows up to 4/i)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Start Challenge/i }));
    await waitFor(() => {
      expect(benchApi.challenges.create).toHaveBeenCalledWith(
        expect.objectContaining({ models: [expect.objectContaining({ effort: "max" })] })
      );
    });
  });

  it("submits effort null when Default stays selected", async () => {
    vi.mocked(benchApi.challenges.create).mockClear();
    renderDialog();
    await fillRequired();
    await screen.findByRole("radio", { name: "Max" });
    await userEvent.click(screen.getByRole("button", { name: /Start Challenge/i }));
    await waitFor(() => {
      expect(benchApi.challenges.create).toHaveBeenCalledWith(
        expect.objectContaining({ models: [expect.objectContaining({ effort: null })] })
      );
    });
  });

  it("shows only Default when no switch had an effect", async () => {
    vi.mocked(benchApi.effortLevels.get).mockResolvedValueOnce({
      served_model_id: "m", reachable: true, status: "no_effect", thinking_key: null,
      effort_param: null, levels: [], default_observed: "on", probed_at: null, serving_since: null,
    });
    renderDialog();
    expect(await screen.findByText(/no switch changed this model/i)).toBeInTheDocument();
    const group = screen.getByRole("radiogroup", { name: /effort for model 1/i });
    expect(group.querySelectorAll('[role="radio"]')).toHaveLength(1);
  });

  it("a failed probe offers Default only and a 'Check again' button", async () => {
    vi.mocked(benchApi.effortLevels.get).mockResolvedValueOnce({
      served_model_id: "m", reachable: true, status: "failed", thinking_key: null,
      effort_param: null, levels: [], default_observed: "unknown", probed_at: null, serving_since: null,
    });
    renderDialog();
    expect(await screen.findByText(/check failed/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /check again/i })).toBeInTheDocument();
  });

  it("an agent row shows the recorded-not-settable note and sends no effort", async () => {
    vi.mocked(api.agents.list).mockResolvedValue([
      { id: "a-1", name: "Worker", model: "some-model", harness: "omp" },
    ] as never);
    vi.mocked(benchApi.challenges.create).mockClear();
    renderDialog();
    await screen.findByRole("option", { name: /bouncing balls/i });
    await userEvent.selectOptions(screen.getByRole("combobox", { name: /source 1/i }), "agent");
    expect(screen.getByText(/recorded after the run, not settable yet/i)).toBeInTheDocument();
    expect(screen.queryByRole("radiogroup", { name: /effort for model 1/i })).toBeNull();
    await userEvent.selectOptions(await screen.findByRole("combobox", { name: /agent 1/i }), "a-1");
    await userEvent.type(screen.getByPlaceholderText(/title/i), "Agent");
    await userEvent.type(screen.getByPlaceholderText(/prompt/i), "Some prompt");
    await userEvent.click(screen.getByRole("button", { name: /Start Challenge/i }));
    await waitFor(() => {
      expect(benchApi.challenges.create).toHaveBeenCalledWith(
        expect.objectContaining({ models: [expect.objectContaining({ source_kind: "agent", effort: null })] })
      );
    });
  });
});

describe("NewChallengeDialog — phone layout (P4, t2 MUSS-4 / SOLL-5, r2 M1)", () => {
  it("the fields do not live in the height-limited scroll box's flex column (they shrank to 18 px)", async () => {
    renderDialog();
    const prompt = await screen.findByPlaceholderText(/prompt/i);
    const scroller = prompt.closest(".overflow-y-auto") as HTMLElement;
    expect(scroller).not.toBeNull();
    // The scroll box is a plain block; the column inside it has no height cap.
    expect(scroller.className).not.toMatch(/\bflex\b/);
  });

  it("mode, series and video length stack on a phone instead of running off-screen", async () => {
    renderDialog();
    const length = await screen.findByLabelText(/Video length/i);
    const row = length.closest("[data-testid='bench-options-row']") as HTMLElement;
    expect(row).not.toBeNull();
    expect(row.className).toContain("flex-col");
    expect(row.className).toContain("sm:flex-row");
  });

  it("each model gets a heading and a divider; remove and add are 44 px targets", async () => {
    renderDialog();
    await userEvent.click(await screen.findByRole("button", { name: /Add model/ }));
    expect(screen.getByText("Model 1")).toBeTruthy();
    expect(screen.getByText("Model 2")).toBeTruthy();
    const second = screen.getByText("Model 2").closest("[data-testid='bench-model-row']") as HTMLElement;
    expect(second.className).toContain("border-t");
    const remove = screen.getByRole("button", { name: "Remove model 1" });
    expect(remove.className).toContain("min-h-11");
    expect(remove.className).toContain("min-w-11");
    expect(screen.getByRole("button", { name: /Add model/ }).className).toContain("min-h-11");
  });

  it("the effort hint describes the chosen level, not Default", async () => {
    renderDialog();
    await userEvent.click(await screen.findByRole("radio", { name: "Off" }));
    expect(screen.queryByText(/Default sends nothing/i)).toBeNull();
    expect(screen.getByText(/Off asks the model not to think/i)).toBeTruthy();
  });
});

describe("NewChallengeDialog — effort check is explicit (round-1 P3)", () => {
  const UNCHECKED = {
    served_model_id: "m", reachable: true, status: "unchecked" as const, thinking_key: null,
    effort_param: null, levels: [], default_observed: "unknown" as const, probed_at: null, serving_since: null,
  };

  it("opening the dialog only reads; an unchecked model offers 'Check now'", async () => {
    vi.mocked(benchApi.effortLevels.get).mockResolvedValueOnce(UNCHECKED);
    vi.mocked(benchApi.effortLevels.check).mockClear();
    renderDialog();
    expect(await screen.findByText(/not checked yet/i)).toBeInTheDocument();
    expect(benchApi.effortLevels.check).not.toHaveBeenCalled();
    const group = screen.getByRole("radiogroup", { name: /effort for model 1/i });
    expect(group.querySelectorAll('[role="radio"]')).toHaveLength(1);
  });

  it("'Check now' probes and then offers the levels found", async () => {
    vi.mocked(benchApi.effortLevels.get).mockResolvedValueOnce(UNCHECKED);
    vi.mocked(benchApi.effortLevels.check).mockResolvedValueOnce({
      ...UNCHECKED, status: "ok", thinking_key: "reasoning_effort", effort_param: "reasoning_effort",
      levels: ["off", "max"], default_observed: "on",
    });
    renderDialog();
    await userEvent.click(await screen.findByRole("button", { name: /check now/i }));
    expect(await screen.findByRole("radio", { name: "Max" })).toBeInTheDocument();
    expect(benchApi.effortLevels.check).toHaveBeenCalledWith(null);
  });

  it("a busy Spark refuses the check and says so", async () => {
    vi.mocked(benchApi.effortLevels.get).mockResolvedValueOnce(UNCHECKED);
    vi.mocked(benchApi.effortLevels.check).mockRejectedValueOnce(
      new Error('API 409: {"detail":"The Spark is busy"}')
    );
    renderDialog();
    await userEvent.click(await screen.findByRole("button", { name: /check now/i }));
    expect(await screen.findByText(/spark is busy/i)).toBeInTheDocument();
  });

  it("the busy hint of one model does not stick after the row switches model (review NICE)", async () => {
    const getLevels = vi.mocked(benchApi.effortLevels.get);
    const original = getLevels.getMockImplementation();
    getLevels.mockResolvedValue(UNCHECKED);
    onTestFinished(() => {
      if (original) getLevels.mockImplementation(original);
    });
    vi.mocked(benchApi.effortLevels.check).mockRejectedValueOnce(
      new Error('API 409: {"detail":"The Spark is busy"}')
    );
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const chips = (model: string) => (
      <QueryClientProvider client={qc}>
        <EffortChips index={0} sourceKind="spark" model={model} sparkOffline={false} value={null} onChange={() => {}} />
      </QueryClientProvider>
    );
    const { rerender } = render(chips("model-a"));
    await userEvent.click(await screen.findByRole("button", { name: /check now/i }));
    expect(await screen.findByText(/spark is busy/i)).toBeInTheDocument();
    rerender(chips("model-b"));
    expect(await screen.findByText(/not checked yet/i)).toBeInTheDocument();
    expect(screen.queryByText(/spark is busy/i)).toBeNull();
  });
});

describe("NewChallengeDialog — labels stay apart (round-1 P3)", () => {
  async function twoRows(labelA: string, labelB: string) {
    renderDialog();
    await screen.findByRole("option", { name: /bouncing balls/i });
    await userEvent.type(screen.getByPlaceholderText(/title/i), "Off vs Max");
    await userEvent.type(screen.getByPlaceholderText(/prompt/i), "Some prompt");
    await userEvent.type(screen.getByRole("textbox", { name: "Label 1" }), labelA);
    await userEvent.click(screen.getByRole("button", { name: /add model/i }));
    await userEvent.type(screen.getByRole("textbox", { name: "Label 2" }), labelB);
    await screen.findAllByRole("radio", { name: "Max" });
    // "Add model" picks the next free level (round-4 K1); these cases are
    // about the same level, so row 2 goes back to Default by hand.
    const defaultOf2 = Array.from(
      screen.getByRole("radiogroup", { name: /effort for model 2/i }).querySelectorAll('[role="radio"]'),
    ).find((c) => c.textContent === "Default") as HTMLElement;
    await userEvent.click(defaultOf2);
  }

  it("same label and same effort: error, Start stays off", async () => {
    await twoRows("DeepSeek", "deepseek ");
    expect(screen.getByText(/same effort as model 1/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Start Challenge/i })).toBeDisabled();
  });

  it("the hint sits inside the model's own block, under its heading (phone layout P4)", async () => {
    await twoRows("DeepSeek", "DeepSeek");
    const hint = screen.getByText(/same effort as model 1/i);
    const block = hint.closest("[data-testid='bench-model-row']") as HTMLElement;
    expect(block).not.toBeNull();
    expect(block.textContent).toContain("Model 2");
  });

  it("same label, different effort: not a duplicate — no hint, the label is sent as typed (round-2 Q2)", async () => {
    vi.mocked(benchApi.challenges.create).mockClear();
    await twoRows("DeepSeek", "DeepSeek");
    const groups = screen.getAllByRole("radiogroup");
    const maxChip = Array.from(groups[1].querySelectorAll('[role="radio"]')).find((c) => c.textContent === "Max");
    await userEvent.click(maxChip as HTMLElement);
    expect(screen.queryByText(/same label/i)).toBeNull();
    expect(screen.queryByText(/added to the label/i)).toBeNull();
    const start = screen.getByRole("button", { name: /Start Challenge/i });
    expect(start).toBeEnabled();
    await userEvent.click(start);
    await waitFor(() => expect(benchApi.challenges.create).toHaveBeenCalled());
    const body = vi.mocked(benchApi.challenges.create).mock.calls[0][0];
    expect(body.models.map((m) => m.label)).toEqual(["DeepSeek", "DeepSeek"]);
    expect(body.models.map((m) => m.effort ?? "default")).toEqual(["default", "max"]);
  });

  it("same label and same effort names both ways out: another effort or an own label", async () => {
    await twoRows("DeepSeek", "DeepSeek");
    expect(screen.getByText(/same label and the same effort as model 1/i)).toBeInTheDocument();
  });
});

describe("NewChallengeDialog — round-4 K1 (R6 rest)", () => {
  const ACTIVE = { reachable: true, models: ["DeepSeek-V4"], active: "DeepSeek-V4" };

  async function fillTitleAndPrompt() {
    await screen.findByRole("option", { name: /bouncing balls/i });
    await userEvent.type(screen.getByPlaceholderText(/title/i), "Off vs Max");
    await userEvent.type(screen.getByPlaceholderText(/prompt/i), "Some prompt");
  }
  const checkedIn = (index: number) =>
    screen
      .getByRole("radiogroup", { name: new RegExp(`effort for model ${index}`, "i") })
      .querySelector('[role="radio"][aria-checked="true"]')?.textContent;

  it("'Add model' takes the next free effort of the same model — no red hint, Start works (t2 SOLL-3)", async () => {
    vi.mocked(benchApi.sparkModels.get).mockResolvedValueOnce(ACTIVE);
    vi.mocked(benchApi.challenges.create).mockClear();
    renderDialog();
    await screen.findByRole("radio", { name: "Max" });
    await fillTitleAndPrompt();
    await userEvent.click(screen.getByRole("button", { name: /add model/i }));
    expect(checkedIn(2)).toBe("Off");
    expect(screen.queryByText(/same effort as model 1/i)).toBeNull();
    const start = screen.getByRole("button", { name: /Start Challenge/i });
    expect(start).toBeEnabled();
    await userEvent.click(start);
    await waitFor(() => expect(benchApi.challenges.create).toHaveBeenCalled());
    const body = vi.mocked(benchApi.challenges.create).mock.calls[0][0];
    expect(body.models.map((m) => m.effort ?? "default")).toEqual(["default", "off"]);
    expect(body.models.map((m) => m.spark_model)).toEqual(["", ""]);
  });

  it("the new row keeps the last row's model and skips every level already used for it", async () => {
    vi.mocked(benchApi.sparkModels.get).mockResolvedValueOnce(ACTIVE);
    renderDialog();
    await screen.findByRole("radio", { name: "Max" });
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "vLLM model 1" }), "DeepSeek-V4");
    await screen.findByRole("radio", { name: "Max" });
    await userEvent.click(screen.getByRole("button", { name: /add model/i }));
    await userEvent.click(screen.getByRole("button", { name: /add model/i }));
    expect((screen.getByRole("combobox", { name: "vLLM model 3" }) as HTMLSelectElement).value).toBe("DeepSeek-V4");
    expect((screen.getByRole("textbox", { name: "Label 3" }) as HTMLInputElement).value).toBe("DeepSeek-V4");
    expect([checkedIn(1), checkedIn(2), checkedIn(3)]).toEqual(["Default", "Off", "Low"]);
    expect(screen.queryByText(/same effort as model/i)).toBeNull();
  });

  it("after Max the next free level wraps round to Default", async () => {
    vi.mocked(benchApi.sparkModels.get).mockResolvedValueOnce(ACTIVE);
    renderDialog();
    await userEvent.click(await screen.findByRole("radio", { name: "Max" }));
    await userEvent.click(screen.getByRole("button", { name: /add model/i }));
    expect(checkedIn(2)).toBe("Default");
  });

  it("an agent row is followed by an agent row without an agent (an agent has only one level)", async () => {
    vi.mocked(api.agents.list).mockResolvedValueOnce([
      { id: "a-1", name: "Sparky", model: "glm", harness: "omp" } as never,
    ]);
    renderDialog();
    await userEvent.selectOptions(await screen.findByRole("combobox", { name: "Source 1" }), "agent");
    await userEvent.selectOptions(await screen.findByRole("combobox", { name: "Agent 1" }), "a-1");
    await userEvent.click(screen.getByRole("button", { name: /add model/i }));
    expect((screen.getByRole("combobox", { name: "Source 2" }) as HTMLSelectElement).value).toBe("agent");
    expect((screen.getByRole("combobox", { name: "Agent 2" }) as HTMLSelectElement).value).toBe("");
    expect(screen.queryByText(/same effort as model 1/i)).toBeNull();
  });

  it("a trailing ' · Max' on a Max row is the same label, like the backend's display_label (t2 KANN-1)", async () => {
    renderDialog();
    await screen.findAllByRole("radio", { name: "Max" });
    await userEvent.type(screen.getByRole("textbox", { name: "Label 1" }), "DeepSeek");
    await userEvent.click(screen.getAllByRole("radio", { name: "Max" })[0]);
    await userEvent.click(screen.getByRole("button", { name: /add model/i }));
    await userEvent.type(screen.getByRole("textbox", { name: "Label 2" }), "DeepSeek · Max");
    const maxOf2 = Array.from(
      screen.getByRole("radiogroup", { name: /effort for model 2/i }).querySelectorAll('[role="radio"]'),
    ).find((c) => c.textContent === "Max") as HTMLElement;
    await userEvent.click(maxOf2);
    expect(screen.getByText(/same effort as model 1/i)).toBeInTheDocument();
  });

  it("'Qwen Max' is a model name, not the effort word — no duplicate", async () => {
    renderDialog();
    await screen.findAllByRole("radio", { name: "Max" });
    await userEvent.type(screen.getByRole("textbox", { name: "Label 1" }), "Qwen");
    await userEvent.click(screen.getAllByRole("radio", { name: "Max" })[0]);
    await userEvent.click(screen.getByRole("button", { name: /add model/i }));
    await userEvent.type(screen.getByRole("textbox", { name: "Label 2" }), "Qwen Max");
    const maxOf2 = Array.from(
      screen.getByRole("radiogroup", { name: /effort for model 2/i }).querySelectorAll('[role="radio"]'),
    ).find((c) => c.textContent === "Max") as HTMLElement;
    await userEvent.click(maxOf2);
    expect(screen.queryByText(/same effort as model 1/i)).toBeNull();
  });

  it("text fields stop at the backend's limits; the title counts down from 180 (t2 SOLL-1)", async () => {
    renderDialog();
    const title = (await screen.findByPlaceholderText(/title/i)) as HTMLInputElement;
    expect(title.maxLength).toBe(200);
    expect((screen.getByRole("textbox", { name: "Label 1" }) as HTMLInputElement).maxLength).toBe(80);
    expect((screen.getByRole("textbox", { name: "Tag 1" }) as HTMLInputElement).maxLength).toBe(80);
    expect((screen.getByPlaceholderText(/series label/i) as HTMLInputElement).maxLength).toBe(80);
    await userEvent.click(title);
    await userEvent.paste("x".repeat(179));
    expect(screen.queryByText("179/200")).toBeNull();
    await userEvent.type(title, "yy");
    expect(screen.getByText("181/200")).toBeInTheDocument();
  });

  it("a 422 on the title is said at the title field, in words, not as a raw toast (t2 SOLL-2)", async () => {
    const { notify } = await import("@/lib/notify");
    vi.mocked(notify.error).mockClear();
    vi.mocked(benchApi.challenges.create).mockRejectedValueOnce(
      new Error(
        'API 422: {"detail":[{"type":"string_too_long","loc":["body","title"],' +
          '"msg":"String should have at most 200 characters","input":"x","ctx":{"max_length":200}}]}',
      ),
    );
    renderDialog();
    await fillTitleAndPrompt();
    await userEvent.type(screen.getByRole("textbox", { name: "Label 1" }), "DeepSeek");
    await userEvent.click(screen.getByRole("button", { name: /Start Challenge/i }));
    const title = screen.getByPlaceholderText(/title/i);
    await waitFor(() => expect(title).toHaveAttribute("aria-invalid", "true"));
    const message = document.getElementById(title.getAttribute("aria-describedby") ?? "");
    expect(message?.textContent).toBe("At most 200 characters.");
    expect(document.activeElement).toBe(title);
    expect(notify.error).not.toHaveBeenCalled();
    // Editing the field takes the message away.
    await userEvent.type(title, "!");
    expect(title).not.toHaveAttribute("aria-invalid");
    expect(screen.queryByText("At most 200 characters.")).toBeNull();
  });

  it("a 422 on a model row's label lands at that row's label", async () => {
    const { notify } = await import("@/lib/notify");
    vi.mocked(notify.error).mockClear();
    vi.mocked(benchApi.challenges.create).mockRejectedValueOnce(
      new Error(
        'API 422: {"detail":[{"type":"string_too_long","loc":["body","models",0,"label"],' +
          '"msg":"String should have at most 80 characters","ctx":{"max_length":80}}]}',
      ),
    );
    renderDialog();
    await fillTitleAndPrompt();
    await userEvent.type(screen.getByRole("textbox", { name: "Label 1" }), "DeepSeek");
    await userEvent.click(screen.getByRole("button", { name: /Start Challenge/i }));
    const label = screen.getByRole("textbox", { name: "Label 1" });
    await waitFor(() => expect(label).toHaveAttribute("aria-invalid", "true"));
    expect(screen.getByText("At most 80 characters.")).toBeInTheDocument();
    expect(notify.error).not.toHaveBeenCalled();
  });

  it("a 422 the dialog has no field for still goes to the toast", async () => {
    const { notify } = await import("@/lib/notify");
    vi.mocked(notify.error).mockClear();
    vi.mocked(benchApi.challenges.create).mockRejectedValueOnce(
      new Error('API 422: {"detail":[{"type":"literal_error","loc":["body","mode"],"msg":"Input should be single"}]}'),
    );
    renderDialog();
    await fillTitleAndPrompt();
    await userEvent.type(screen.getByRole("textbox", { name: "Label 1" }), "DeepSeek");
    await userEvent.click(screen.getByRole("button", { name: /Start Challenge/i }));
    await waitFor(() => expect(notify.error).toHaveBeenCalledWith("mode: Input should be single"));
  });

  it("every select draws its own box: appearance none and 44 px, also in WebKit (t2 SOLL-5)", async () => {
    vi.mocked(benchApi.sparkModels.get).mockResolvedValueOnce(ACTIVE);
    renderDialog();
    await screen.findByRole("combobox", { name: "vLLM model 1" });
    const selects = screen.getAllByRole("combobox");
    expect(selects.length).toBeGreaterThanOrEqual(4);
    for (const s of selects) {
      expect(s.className, s.getAttribute("aria-label") ?? "").toContain("appearance-none");
      expect(s.className).toContain("min-h-11");
    }
  });

  it("the tag placeholder says the default in the UI language, not 'LOCAL · DIRECT API' (r2 N7)", async () => {
    renderDialog();
    const tag = await screen.findByRole("textbox", { name: "Tag 1" });
    expect(tag.getAttribute("placeholder")).toBe("Tag (empty: local · direct API)");
  });
});
