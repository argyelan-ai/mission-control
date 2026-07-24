"use client";

import { useState, useMemo } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { Sparkles, Send, AlertCircle } from "lucide-react";
import { C, STATUS_TEXT } from "@/lib/colors";

/**
 * 4-field form for reflection comments (Phase G, 2026-04-12).
 *
 * Fields:
 * 1. What was done
 * 2. What worked
 * 3. What was unclear / could have gone better
 * 4. Lesson (auto-saved to agent memory via the Phase B pipeline — the
 *    backend extractor anchors on the "Lesson" heading, keep the word)
 *
 * Builds the markdown string and calls onSubmit(content).
 * Min length: 80 chars (backend rule of 4 requirements).
 */

interface ReflectionFormProps {
  onSubmit: (content: string) => void;
  isSubmitting?: boolean;
}

const FIELDS = [
  {
    key: "done",
    label: "What was done",
    placeholder: "Facts — what actually happened",
    heading: "What was done",
  },
  {
    key: "worked",
    label: "What worked",
    placeholder: "Success signals, what went well",
    heading: "What worked",
  },
  {
    key: "unclear",
    label: "What was unclear",
    placeholder: "What could have gone better, uncertainties",
    heading: "What was unclear",
  },
  {
    key: "lesson",
    label: "Lesson (saved to memory)",
    placeholder: "What should go differently next time — this part is automatically saved as an agent lesson",
    heading: "Lesson",
  },
] as const;

export function ReflectionForm({ onSubmit, isSubmitting }: ReflectionFormProps) {
  const [values, setValues] = useState<Record<string, string>>({
    done: "",
    worked: "",
    unclear: "",
    lesson: "",
  });

  const markdown = useMemo(() => {
    return FIELDS.map(
      (f) => `## ${f.heading}\n${values[f.key].trim() || "_—_"}`
    ).join("\n\n");
  }, [values]);

  const totalChars = Object.values(values).reduce((sum, v) => sum + v.trim().length, 0);
  const isValid = totalChars >= 80 && FIELDS.every((f) => values[f.key].trim().length > 0);

  function handleSubmit() {
    if (!isValid || isSubmitting) return;
    onSubmit(markdown);
    setValues({ done: "", worked: "", unclear: "", lesson: "" });
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2 mb-1">
        <Sparkles size={14} style={{ color: STATUS_TEXT.info }} />
        <span className="text-[11px] font-medium" style={{ color: STATUS_TEXT.info }}>
          Self-Reflection
        </span>
        <span className="text-[10px]" style={{ color: C.textMuted }}>
          4 required fields — min. 80 characters total
        </span>
      </div>

      {FIELDS.map((field) => (
        <div key={field.key}>
          <label
            className="block text-[10px] font-medium mb-1"
            style={{ color: field.key === "lesson" ? STATUS_TEXT.info : C.textMuted }}
          >
            {field.label}
          </label>
          <textarea
            value={values[field.key]}
            onChange={(e) =>
              setValues((prev) => ({ ...prev, [field.key]: e.target.value }))
            }
            placeholder={field.placeholder}
            rows={field.key === "lesson" ? 3 : 2}
            className="w-full px-3 py-2 rounded-lg text-xs outline-none resize-none transition-colors"
            style={{
              backgroundColor: "var(--color-bg-surface)",
              color: C.textPrimary,
              border: `1px solid ${
                field.key === "lesson"
                  ? `${C.info}33`
                  : C.border
              }`,
            }}
          />
        </div>
      ))}

      <div className="flex items-center justify-between">
        <AnimatePresence>
          {totalChars > 0 && totalChars < 80 && (
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              className="flex items-center gap-1 text-[10px]"
              style={{ color: C.warning }}
            >
              <AlertCircle size={10} />
              <span>{80 - totalChars} more characters to minimum length</span>
            </motion.div>
          )}
        </AnimatePresence>

        <button
          onClick={handleSubmit}
          disabled={!isValid || isSubmitting}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-[11px] font-medium transition-all cursor-pointer disabled:opacity-30 disabled:cursor-not-allowed"
          style={{
            background: isValid ? `${C.info}26` : "rgba(255, 255, 255, 0.03)",
            color: isValid ? STATUS_TEXT.info : C.textMuted,
            border: `1px solid ${isValid ? `${C.info}4D` : C.border}`,
          }}
        >
          <Send size={11} />
          Post reflection
        </button>
      </div>
    </div>
  );
}
