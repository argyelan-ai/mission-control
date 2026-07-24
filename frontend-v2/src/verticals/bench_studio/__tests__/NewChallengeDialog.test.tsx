import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { PromptTemplate } from "../types";

vi.mock("@/verticals/bench_studio/api", () => ({
  benchApi: {
    sparkModels: {
      get: vi.fn().mockResolvedValue({ reachable: true, models: [], active: null }),
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
    expect(screen.getByRole("option", { name: /freitext/i })).toBeTruthy();
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
    const titleInput = screen.getByPlaceholderText(/titel/i);
    await userEvent.type(titleInput, "My Test");

    // Fill model label (required for valid form)
    const modelLabelInput = screen.getByPlaceholderText(/Label \(z\. B\./i);
    await userEvent.type(modelLabelInput, "DeepSeek");

    // Submit
    const submitBtn = screen.getByRole("button", { name: /Challenge starten/i });
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
    const titleInput = screen.getByPlaceholderText(/titel/i);
    await userEvent.type(titleInput, "My Test");
    const modelLabelInput = screen.getByPlaceholderText(/Label \(z\. B\./i);
    await userEvent.type(modelLabelInput, "DeepSeek");

    const submitBtn = screen.getByRole("button", { name: /Challenge starten/i });
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
    await userEvent.type(screen.getByPlaceholderText(/titel/i), "Tag Test");
    const textarea = screen.getByPlaceholderText(/prompt/i);
    await userEvent.type(textarea, "Some prompt");
    await userEvent.type(screen.getByPlaceholderText(/Label \(z\. B\./i), "Qwen");

    // Tag input shows the derived default as placeholder (spark row)
    const tagInput = screen.getByRole("textbox", { name: /tag 1/i });
    expect((tagInput as HTMLInputElement).placeholder).toContain("VLLM · SPARK");

    // Type a custom tag
    await userEvent.type(tagInput, "OMP · DGX SPARK");

    await userEvent.click(screen.getByRole("button", { name: /Challenge starten/i }));

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

    await userEvent.type(screen.getByPlaceholderText(/titel/i), "Tag Test");
    await userEvent.type(screen.getByPlaceholderText(/prompt/i), "Some prompt");
    await userEvent.type(screen.getByPlaceholderText(/Label \(z\. B\./i), "Qwen");

    await userEvent.click(screen.getByRole("button", { name: /Challenge starten/i }));

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
    await userEvent.type(screen.getByPlaceholderText(/titel/i), "My Test");

    // Fill first model — spark model via the select (Bench #21: fed by
    // benchApi.sparkModels.get). aria-label, not placeholder: the auto row's
    // placeholder previews the live active model and isn't a fixed string.
    const labelInput = screen.getByRole("textbox", { name: /label 1/i });
    await userEvent.type(labelInput, "DeepSeek");
    const sparkSelect = await screen.findByRole("combobox", { name: /vLLM-Modell 1/i });
    await userEvent.selectOptions(sparkSelect, "deepseek-v4");

    // Now submit button should be enabled
    const submitBtn = screen.getByRole("button", { name: /Challenge starten/i });
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

    const durationInput = screen.getByLabelText(/video-länge/i) as HTMLInputElement;
    expect(durationInput.value).toBe("20");

    await userEvent.type(screen.getByPlaceholderText(/titel/i), "Duration Test");
    await userEvent.type(screen.getByPlaceholderText(/prompt/i), "Some prompt");
    await userEvent.type(screen.getByPlaceholderText(/Label \(z\. B\./i), "Qwen");

    await userEvent.click(screen.getByRole("button", { name: /Challenge starten/i }));

    await waitFor(() => {
      expect(benchApi.challenges.create).toHaveBeenCalledWith(
        expect.objectContaining({ record_duration_s: 20 })
      );
    });
  });

  it("sends a custom video length value", async () => {
    renderDialog();
    await screen.findByRole("option", { name: /bouncing balls/i });

    const durationInput = screen.getByLabelText(/video-länge/i) as HTMLInputElement;
    await userEvent.clear(durationInput);
    await userEvent.type(durationInput, "45");

    await userEvent.type(screen.getByPlaceholderText(/titel/i), "Duration Test 2");
    await userEvent.type(screen.getByPlaceholderText(/prompt/i), "Some prompt");
    await userEvent.type(screen.getByPlaceholderText(/Label \(z\. B\./i), "Qwen");

    await userEvent.click(screen.getByRole("button", { name: /Challenge starten/i }));

    await waitFor(() => {
      expect(benchApi.challenges.create).toHaveBeenCalledWith(
        expect.objectContaining({ record_duration_s: 45 })
      );
    });
  });

  it("clears the field to empty and still submits the 20s default (not a raw empty value)", async () => {
    renderDialog();
    await screen.findByRole("option", { name: /bouncing balls/i });

    const durationInput = screen.getByLabelText(/video-länge/i) as HTMLInputElement;
    await userEvent.clear(durationInput);
    expect(durationInput.value).toBe("");

    await userEvent.type(screen.getByPlaceholderText(/titel/i), "Duration Test 3");
    await userEvent.type(screen.getByPlaceholderText(/prompt/i), "Some prompt");
    await userEvent.type(screen.getByPlaceholderText(/Label \(z\. B\./i), "Qwen");

    await userEvent.click(screen.getByRole("button", { name: /Challenge starten/i }));

    await waitFor(() => {
      expect(benchApi.challenges.create).toHaveBeenCalledWith(
        expect.objectContaining({ record_duration_s: 20 })
      );
    });
  });

  it("blurring the empty field re-normalizes the displayed value to 20", async () => {
    renderDialog();
    await screen.findByRole("option", { name: /bouncing balls/i });

    const durationInput = screen.getByLabelText(/video-länge/i) as HTMLInputElement;
    await userEvent.clear(durationInput);
    await userEvent.tab(); // blur

    expect(durationInput.value).toBe("20");
  });

  it("clamps an out-of-range typed value (e.g. 200) down to the 60s max on blur", async () => {
    renderDialog();
    await screen.findByRole("option", { name: /bouncing balls/i });

    const durationInput = screen.getByLabelText(/video-länge/i) as HTMLInputElement;
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

    const durationInput = screen.getByLabelText(/video-länge/i) as HTMLInputElement;
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

    const sparkSelect = await screen.findByRole("combobox", { name: /vLLM-Modell 1/i });
    await userEvent.selectOptions(sparkSelect, "deepseek-v4");

    const labelInput = screen.getByRole("textbox", { name: /label 1/i });
    expect((labelInput as HTMLInputElement).value).toBe("deepseek-v4");
  });

  it("choosing 'Aktives Modell (auto)' leaves the label empty, showing the active model only as a placeholder (review MINOR 1)", async () => {
    vi.mocked(benchApi.sparkModels.get).mockResolvedValueOnce({
      reachable: true, models: ["deepseek-v4", "qwen3.6"], active: "qwen3.6",
    });
    renderDialog();
    await screen.findByRole("option", { name: /bouncing balls/i });

    const sparkSelect = await screen.findByRole("combobox", { name: /vLLM-Modell 1/i });
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
    await screen.findByRole("combobox", { name: /vLLM-Modell 1/i }); // wait for the spark-models query

    const labelInput = screen.getByRole("textbox", { name: /label 1/i });
    await userEvent.type(labelInput, "Mein Auto-Label");

    await userEvent.type(screen.getByPlaceholderText(/titel/i), "Hand-Edited Label Test");
    await userEvent.type(screen.getByPlaceholderText(/prompt/i), "Some prompt");
    await userEvent.click(screen.getByRole("button", { name: /Challenge starten/i }));

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
    await userEvent.selectOptions(screen.getByRole("combobox", { name: /quelle 1/i }), "agent");
    await userEvent.selectOptions(await screen.findByRole("combobox", { name: /agent 1/i }), "agent-1");

    const labelInput = screen.getByRole("textbox", { name: /label 1/i });
    expect((labelInput as HTMLInputElement).value).toBe("Qwen/Qwen3.6-35B-A3B-FP8");
  });

  it("a hand-edited label is not overwritten by later selection", async () => {
    renderDialog();
    await screen.findByRole("option", { name: /bouncing balls/i });

    const labelInput = screen.getByRole("textbox", { name: /label 1/i });
    await userEvent.type(labelInput, "Mein Label");

    await userEvent.selectOptions(screen.getByRole("combobox", { name: /quelle 1/i }), "agent");
    await userEvent.selectOptions(await screen.findByRole("combobox", { name: /agent 1/i }), "agent-1");

    expect((labelInput as HTMLInputElement).value).toBe("Mein Label");
  });
});

describe("NewChallengeDialog — vanilla (direct-API) spark row (Bench #21)", () => {
  it("labels the source option 'Direkt-API (vanilla)' instead of 'Spark'", async () => {
    renderDialog();
    await screen.findByRole("option", { name: /bouncing balls/i });
    expect(screen.getByRole("option", { name: /direkt-api \(vanilla\)/i })).toBeTruthy();
    expect(screen.queryByRole("option", { name: /^spark$/i })).toBeNull();
  });

  it("shows a select fed by benchApi.sparkModels.get with an auto option first", async () => {
    vi.mocked(benchApi.sparkModels.get).mockResolvedValueOnce({
      reachable: true, models: ["deepseek-v4", "qwen3.6"], active: "qwen3.6",
    });
    renderDialog();
    await screen.findByRole("option", { name: /bouncing balls/i });

    const sparkSelect = await screen.findByRole("combobox", { name: /vLLM-Modell 1/i });
    const options = Array.from(sparkSelect.querySelectorAll("option")).map((o) => o.textContent);
    expect(options[0]).toMatch(/aktives modell \(auto\)/i);
    expect(options).toContain("deepseek-v4");
    expect(options).toContain("qwen3.6");
  });

  it("choosing the auto option with an untouched label submits label='' and spark_model='' (backend resolves+fills both at create)", async () => {
    vi.mocked(benchApi.sparkModels.get).mockResolvedValueOnce({
      reachable: true, models: ["qwen3.6"], active: "qwen3.6",
    });
    renderDialog();
    await screen.findByRole("option", { name: /bouncing balls/i });
    await screen.findByRole("combobox", { name: /vLLM-Modell 1/i }); // wait for the spark-models query

    // Auto is the default selection ("") and the label is left untouched —
    // review MINOR 1: this must stay VALID (submit enabled) and must NOT
    // carry the dialog-load-time active model into the submitted label.
    await userEvent.type(screen.getByPlaceholderText(/titel/i), "Auto Test");
    await userEvent.type(screen.getByPlaceholderText(/prompt/i), "Some prompt");

    const submitBtn = screen.getByRole("button", { name: /Challenge starten/i });
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

    await screen.findByText(/spark offline — modell manuell eintragen oder später starten/i);
    const fallbackInput = screen.getByPlaceholderText(/vLLM-Modell \(leer = aktiv\)/);
    expect(fallbackInput.tagName).toBe("INPUT");
    expect(screen.queryByRole("combobox", { name: /vLLM-Modell 1/i })).toBeNull();

    await userEvent.type(screen.getByPlaceholderText(/titel/i), "Offline Test");
    await userEvent.type(screen.getByPlaceholderText(/prompt/i), "Some prompt");
    await userEvent.type(screen.getByPlaceholderText(/Label \(z\. B\./), "DeepSeek");

    // NIT 2: still on "auto" (spark_model empty) while confirmed offline —
    // create() would 422 immediately, so submit must stay disabled even
    // though title/prompt/label are all filled in.
    expect(screen.getByRole("button", { name: /Challenge starten/i })).toBeDisabled();

    // Typing a model manually (free-text fallback) is no longer "auto" —
    // submit re-enables.
    await userEvent.type(fallbackInput, "manual-model");
    expect((fallbackInput as HTMLInputElement).value).toBe("manual-model");
    expect(screen.getByRole("button", { name: /Challenge starten/i })).not.toBeDisabled();
  });

  it("treats a spark-models query ERROR the same as unreachable (review NIT 1)", async () => {
    vi.mocked(benchApi.sparkModels.get).mockRejectedValueOnce(new Error("network down"));
    renderDialog();
    await screen.findByRole("option", { name: /bouncing balls/i });

    // Must fall back to the free-text warning UI, not a trap select with
    // only the auto option and no visible warning.
    await screen.findByText(/spark offline/i);
    expect(screen.getByPlaceholderText(/vLLM-Modell \(leer = aktiv\)/).tagName).toBe("INPUT");
    expect(screen.queryByRole("combobox", { name: /vLLM-Modell 1/i })).toBeNull();
  });

  it("surfaces the backend's error detail in the failure toast (review MINOR 2)", async () => {
    const { notify } = await import("@/lib/notify");
    vi.mocked(benchApi.challenges.create).mockRejectedValueOnce(
      new Error('API 422: {"detail":"Spark nicht erreichbar — Modell nicht auflösbar"}')
    );
    renderDialog();
    await screen.findByRole("option", { name: /bouncing balls/i });

    await userEvent.type(screen.getByPlaceholderText(/titel/i), "Error Toast Test");
    await userEvent.type(screen.getByPlaceholderText(/prompt/i), "Some prompt");
    await userEvent.type(screen.getByPlaceholderText(/Label \(z\. B\./), "DeepSeek");
    await userEvent.click(screen.getByRole("button", { name: /Challenge starten/i }));

    await waitFor(() => {
      expect(notify.error).toHaveBeenCalledWith("Spark nicht erreichbar — Modell nicht auflösbar");
    });
  });

  it("falls back to a generic message when the error body isn't the API detail shape", async () => {
    const { notify } = await import("@/lib/notify");
    vi.mocked(benchApi.challenges.create).mockRejectedValueOnce(new Error("network exploded"));
    renderDialog();
    await screen.findByRole("option", { name: /bouncing balls/i });

    await userEvent.type(screen.getByPlaceholderText(/titel/i), "Error Toast Fallback Test");
    await userEvent.type(screen.getByPlaceholderText(/prompt/i), "Some prompt");
    await userEvent.type(screen.getByPlaceholderText(/Label \(z\. B\./), "DeepSeek");
    await userEvent.click(screen.getByRole("button", { name: /Challenge starten/i }));

    await waitFor(() => {
      expect(notify.error).toHaveBeenCalledWith("Challenge konnte nicht gestartet werden");
    });
  });
});
