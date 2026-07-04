"use client";

import { useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { Search, Sparkles, Loader2, X } from "lucide-react";
import { api } from "@/lib/api";
import { C, STATUS_TEXT } from "@/lib/colors";
import { LAYER_COLORS } from "@/components/memory/graphConfig";

/**
 * Semantic Memory Search (Phase 3/4, 2026-04-11).
 *
 * Nutzt das neue POST /api/v1/memory/query Endpoint (Qdrant + Spark-Embedding).
 * Suche laeuft ueber alle 3 Layer (semantic/agent/episodic) und zeigt Treffer
 * mit Similarity-Score. Bei Embedding-Fail (Spark down) fallback auf ILIKE.
 */

type Layer = "semantic" | "agent" | "episodic";

type Hit = {
  memory_id: string;
  score: number;
  title: string;
  content_preview: string;
  memory_type?: string;
  tags?: string[];
  source: string;
};

type QueryResponse = {
  query: string;
  fallback?: boolean;
  results: Record<string, Hit[]>;
};

const LAYER_LABELS: Record<Layer, { label: string; color: string }> = {
  semantic: { label: "Semantic",      color: LAYER_COLORS.semantic },
  agent:    { label: "Agent Lessons", color: LAYER_COLORS.agent },
  episodic: { label: "Episodic",      color: LAYER_COLORS.episodic },
};

export function MemoryQueryBar({ boardId, agentId }: { boardId?: string | null; agentId?: string | null }) {
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<QueryResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function runQuery() {
    if (!query.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const res = await api.knowledge.query({
        query: query.trim(),
        layers: ["semantic", "agent", "episodic"],
        top_k: 5,
        agent_id: agentId || null,
        board_id: boardId || null,
      });
      setResult(res as QueryResponse);
    } catch (e: any) {
      setError(e?.message || "Query fehlgeschlagen");
    } finally {
      setLoading(false);
    }
  }

  function clear() {
    setQuery("");
    setResult(null);
    setError(null);
  }

  const totalHits = result
    ? Object.values(result.results).reduce((sum, hits) => sum + hits.length, 0)
    : 0;

  return (
    <div className="mb-6">
      <div
        className="flex items-center gap-3 px-4 py-3 rounded-xl border"
        style={{
          background: C.accentSubtle,
          borderColor: C.borderAccent,
        }}
      >
        <Sparkles size={18} style={{ color: C.accent }} className="flex-shrink-0" />
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") runQuery();
            if (e.key === "Escape") clear();
          }}
          placeholder="Semantic Memory Search — frag das Memory (Qdrant + Spark-Embedding)..."
          className="flex-1 bg-transparent outline-none text-sm text-white placeholder:text-white/30"
        />
        {query && (
          <button
            onClick={clear}
            className="p-1 rounded hover:bg-white/5 text-white/40 hover:text-white/70"
            aria-label="Clear"
          >
            <X size={14} />
          </button>
        )}
        <button
          onClick={runQuery}
          disabled={!query.trim() || loading}
          className="flex items-center gap-2 px-3 py-1.5 rounded-lg text-xs font-medium disabled:opacity-40 transition-colors"
          style={{
            background: C.accentSubtle,
            color: C.accent,
            border: `1px solid ${C.borderAccent}`,
          }}
        >
          {loading ? <Loader2 size={12} className="animate-spin" /> : <Search size={12} />}
          Suchen
        </button>
      </div>

      <AnimatePresence>
        {error && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: "auto" }}
            exit={{ opacity: 0, height: 0 }}
            className="mt-2 px-4 py-2 text-xs rounded-lg"
            style={{ background: "rgba(194,56,56,0.12)", color: STATUS_TEXT.error }}
          >
            {error}
          </motion.div>
        )}

        {result && (
          <motion.div
            initial={{ opacity: 0, y: -8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -8 }}
            className="mt-3 space-y-3"
          >
            <div className="flex items-center justify-between px-1">
              <span className="text-xs text-white/40">
                {totalHits} Treffer fuer &ldquo;{result.query}&rdquo;
                {result.fallback && (
                  <span className="ml-2 px-1.5 py-0.5 rounded text-[10px]" style={{ background: "rgba(184,135,10,0.15)", color: STATUS_TEXT.warning }}>
                    keyword fallback
                  </span>
                )}
              </span>
            </div>

            {(["semantic", "agent", "episodic"] as Layer[]).map((layer) => {
              const hits = result.results[layer] || [];
              if (hits.length === 0) return null;
              const cfg = LAYER_LABELS[layer];
              return (
                <div key={layer} className="space-y-1.5">
                  <div className="flex items-center gap-2 px-1">
                    <div
                      className="w-1.5 h-1.5 rounded-full"
                      style={{ background: cfg.color }}
                    />
                    <span className="text-[11px] font-medium uppercase tracking-wider" style={{ color: cfg.color }}>
                      {cfg.label}
                    </span>
                    <span className="text-[11px] text-white/30">{hits.length}</span>
                  </div>
                  <div className="space-y-1.5">
                    {hits.map((hit) => (
                      <div
                        key={hit.memory_id}
                        className="px-3 py-2 rounded-lg border text-xs hover:bg-white/[0.03] transition-colors cursor-default"
                        style={{
                          background: "rgba(255,255,255,0.02)",
                          borderColor: "rgba(255,255,255,0.06)",
                        }}
                      >
                        <div className="flex items-start justify-between gap-2 mb-1">
                          <div className="font-medium text-white/90 truncate">
                            {hit.title || "(Ohne Titel)"}
                          </div>
                          <div className="flex items-center gap-2 flex-shrink-0">
                            {hit.memory_type && (
                              <span className="text-[10px] text-white/40 uppercase">
                                {hit.memory_type}
                              </span>
                            )}
                            <span
                              className="text-[10px] font-mono px-1.5 py-0.5 rounded"
                              style={{
                                background: hit.score > 0.75 ? "rgba(0,204,136,0.15)" : "rgba(255,255,255,0.05)",
                                color: hit.score > 0.75 ? C.online : C.textSecondary,
                              }}
                            >
                              {hit.score.toFixed(3)}
                            </span>
                          </div>
                        </div>
                        <div className="text-white/50 line-clamp-2">
                          {hit.content_preview}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              );
            })}

            {totalHits === 0 && (
              <div className="px-4 py-6 text-center text-xs text-white/30">
                Keine Treffer — versuch eine andere Formulierung
              </div>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
