"use client";

import { useState } from "react";
import { motion } from "framer-motion";
import { X, ChevronLeft, ChevronRight, Check } from "lucide-react";
import { C } from "@/lib/colors";
import type { Board } from "@/lib/types";
import {
  WIZARD_STEPS,
  canProceed,
  initialWizardState,
  type WizardState,
  type WizardStepProps,
} from "./types";
import {
  wizardOverlayClass,
  wizardBackdropClass,
  wizardCardStyle,
  wizardBtnPrimaryStyle,
} from "./shared";

const ease = [0.16, 1, 0.3, 1] as const;

// Step components are registered in Tasks 9-13. Until then a step renders a
// placeholder so the shell + navigation are independently testable.
type StepComponent = (props: WizardStepProps) => React.ReactNode;

function Placeholder({ state }: WizardStepProps) {
  return (
    <div className="text-sm text-[var(--color-text-muted)]">
      Schritt {state.step + 1} — in Arbeit.
    </div>
  );
}

// Populated in Task 13 (wiring). Keyed by WIZARD_STEPS index.
export const WIZARD_STEP_COMPONENTS: StepComponent[] = [
  Placeholder,
  Placeholder,
  Placeholder,
  Placeholder,
  Placeholder,
];

export function AgentWizard({
  boards,
  defaultBoardId,
  initialState,
  onClose,
  onCreated,
}: {
  boards: Board[];
  defaultBoardId: string | null;
  initialState?: Partial<WizardState>;
  onClose: () => void;
  onCreated: () => void;
}) {
  const [state, setState] = useState<WizardState>(() => ({
    ...initialWizardState(defaultBoardId),
    ...initialState,
  }));

  const update = (patch: Partial<WizardState>) =>
    setState((s) => ({ ...s, ...patch }));
  const goNext = () =>
    setState((s) => ({ ...s, step: Math.min(s.step + 1, WIZARD_STEPS.length - 1) }));
  const goBack = () => setState((s) => ({ ...s, step: Math.max(s.step - 1, 0) }));

  const StepComponent = WIZARD_STEP_COMPONENTS[state.step] ?? Placeholder;
  const isLastStep = state.step === WIZARD_STEPS.length - 1;

  return (
    <div className={wizardOverlayClass} onClick={onClose}>
      <div className={wizardBackdropClass} />
      <motion.div
        initial={{ opacity: 0, scale: 0.97, y: 8 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.97, y: 8 }}
        transition={{ duration: 0.2, ease }}
        className="relative w-full max-w-2xl rounded-t-2xl sm:rounded-2xl overflow-hidden max-h-[92dvh] flex flex-col"
        style={wizardCardStyle}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header + stepper */}
        <div className="px-5 py-4 border-b" style={{ borderColor: C.borderSubtle }}>
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-sm font-semibold text-[var(--color-text-primary)]">
              Neuer Agent
            </h2>
            <button
              onClick={onClose}
              aria-label="Wizard schliessen"
              className="cursor-pointer text-[var(--color-text-muted)] hover:text-[var(--color-text-secondary)] transition-colors"
            >
              <X size={16} />
            </button>
          </div>
          <div className="flex items-center gap-1.5">
            {WIZARD_STEPS.map((s, i) => {
              const active = i === state.step;
              const done = i < state.step;
              return (
                <div key={s.key} className="flex items-center gap-1.5 flex-1 last:flex-none">
                  <div className="flex items-center gap-1.5">
                    <div
                      className="flex items-center justify-center w-5 h-5 rounded-full text-[10px] font-medium shrink-0"
                      style={{
                        backgroundColor: active || done ? C.accent : "rgba(255,255,255,0.06)",
                        color: active || done ? "#fff" : "var(--color-text-muted)",
                      }}
                    >
                      {done ? <Check size={11} /> : i + 1}
                    </div>
                    <span
                      className="text-[11px] hidden sm:inline"
                      style={{
                        color: active
                          ? "var(--color-text-primary)"
                          : "var(--color-text-muted)",
                      }}
                    >
                      {s.label}
                    </span>
                  </div>
                  {i < WIZARD_STEPS.length - 1 && (
                    <div
                      className="flex-1 h-px min-w-[8px]"
                      style={{ backgroundColor: done ? C.accent : "rgba(255,255,255,0.08)" }}
                    />
                  )}
                </div>
              );
            })}
          </div>
        </div>

        {/* Step body */}
        <div className="p-5 overflow-y-auto flex-1">
          <StepComponent
            state={state}
            update={update}
            boards={boards}
            goNext={goNext}
            goBack={goBack}
          />
        </div>

        {/* Footer nav — the review step owns its own primary action, so hide
            "Weiter" there. */}
        <div
          className="flex items-center justify-between px-5 py-4 border-t"
          style={{ borderColor: C.borderSubtle }}
        >
          <button
            onClick={goBack}
            disabled={state.step === 0}
            className="flex items-center gap-1.5 px-4 py-2 text-sm rounded-xl cursor-pointer transition-colors text-[var(--color-text-secondary)] hover:text-[var(--color-text-primary)] disabled:opacity-30 disabled:cursor-not-allowed"
          >
            <ChevronLeft size={15} /> Zurück
          </button>
          {!isLastStep && (
            <button
              onClick={goNext}
              disabled={!canProceed(state)}
              className="flex items-center gap-1.5 px-5 py-2 text-sm rounded-xl font-medium text-white disabled:opacity-40 disabled:cursor-not-allowed cursor-pointer transition-all"
              style={wizardBtnPrimaryStyle}
            >
              Weiter <ChevronRight size={15} />
            </button>
          )}
        </div>
      </motion.div>
    </div>
  );
}
