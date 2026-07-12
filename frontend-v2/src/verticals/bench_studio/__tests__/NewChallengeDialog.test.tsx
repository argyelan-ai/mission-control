import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { PromptTemplate } from "../types";

vi.mock("@/verticals/bench_studio/api", () => ({
  benchApi: {
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
      status: "generating", series_label: null, series_no: null,
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
      status: "generating", series_label: null, series_no: null,
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
      status: "generating", series_label: null, series_no: null,
      composed_video_path: null, content_pipeline_id: null, error: null, archived_at: null,
      created_at: "", updated_at: "", entries: [],
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

    // Fill first model (label + spark model)
    const [labelInput, sparkInput] = screen.getAllByPlaceholderText(/Label \(z\. B\.|vLLM-Modell/);
    await userEvent.type(labelInput, "DeepSeek");
    await userEvent.type(sparkInput, "deepseek-v4");

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
