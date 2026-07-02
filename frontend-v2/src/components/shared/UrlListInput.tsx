"use client";

import { useState } from "react";
import { Plus, X } from "lucide-react";

interface UrlListInputProps {
  value: string[];
  onChange: (urls: string[]) => void;
  textPrimary?: string;
  textMuted?: string;
  border?: string;
  deep?: string;
  accent?: string;
  error?: string;
}

export function UrlListInput({
  value,
  onChange,
  textPrimary = "#EDEDEF",
  textMuted = "#888888",
  border = "rgba(255,255,255,0.06)",
  deep = "#020203",
  accent = "#0FA3A3",
}: UrlListInputProps) {
  const [input, setInput] = useState("");

  const addUrl = () => {
    const url = input.trim();
    if (!url) return;
    if (!url.startsWith("http://") && !url.startsWith("https://")) return;
    if (value.includes(url)) return;
    onChange([...value, url]);
    setInput("");
  };

  const removeUrl = (index: number) => {
    onChange(value.filter((_, i) => i !== index));
  };

  return (
    <div className="flex flex-col gap-1.5">
      {value.map((url, i) => (
        <div
          key={i}
          className="flex items-center gap-2 px-2.5 py-1.5 rounded-lg text-[11px]"
          style={{ backgroundColor: `${deep}88`, border: `1px solid ${border}` }}
        >
          <span className="flex-1 truncate" style={{ color: textPrimary }}>{url}</span>
          <button
            type="button"
            onClick={() => removeUrl(i)}
            aria-label="URL entfernen"
            className="shrink-0 cursor-pointer hover:opacity-80"
          >
            <X size={10} style={{ color: textMuted }} />
          </button>
        </div>
      ))}
      <div className="flex items-center gap-1.5">
        <input
          aria-label="Referenz-URL eingeben"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); addUrl(); } }}
          placeholder="https://..."
          className="flex-1 text-[11px] px-2.5 py-1.5 rounded-lg bg-transparent outline-none"
          style={{ border: `1px solid ${border}`, color: textPrimary }}
        />
        <button
          type="button"
          onClick={addUrl}
          aria-label="URL hinzufügen"
          disabled={!input.trim()}
          className="px-2 py-1.5 rounded-lg cursor-pointer transition-opacity disabled:opacity-30"
          style={{ backgroundColor: `${accent}11`, border: `1px solid ${border}` }}
        >
          <Plus size={12} style={{ color: accent }} />
        </button>
      </div>
    </div>
  );
}
