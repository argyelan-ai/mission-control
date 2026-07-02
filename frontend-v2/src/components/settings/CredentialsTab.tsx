"use client";

import { useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { Plus, Pencil, Trash2, Eye, EyeOff, KeyRound, FileText, X } from "lucide-react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import type { Credential } from "@/lib/types";
import { C } from "@/lib/colors";

const TYPE_CONFIG = {
  login: { label: "Login", icon: KeyRound, color: C.accent },
  token: { label: "Token", icon: KeyRound, color: C.warning },
  custom: { label: "Freitext", icon: FileText, color: C.textSecondary },
};

type CredentialType = "login" | "token" | "custom";

interface ModalState {
  open: boolean;
  editing: Credential | null;
}

export function CredentialsTab() {
  const qc = useQueryClient();
  const [modal, setModal] = useState<ModalState>({ open: false, editing: null });
  const [deleteConfirm, setDeleteConfirm] = useState<string | null>(null);

  // Form state
  const [name, setName] = useState("");
  const [credType, setCredType] = useState<CredentialType>("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [token, setToken] = useState("");
  const [content, setContent] = useState("");
  const [url, setUrl] = useState("");
  const [notes, setNotes] = useState("");
  const [showPassword, setShowPassword] = useState(false);

  const { data: credentials, isLoading } = useQuery({
    queryKey: ["credentials"],
    queryFn: () => api.credentials.list(),
  });

  const createMut = useMutation({
    mutationFn: (data: Parameters<typeof api.credentials.create>[0]) => api.credentials.create(data),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["credentials"] }); notify.success("Credential erstellt"); closeModal(); },
    onError: () => notify.error("Fehler beim Erstellen"),
  });

  const updateMut = useMutation({
    mutationFn: ({ id, data }: { id: string; data: Parameters<typeof api.credentials.update>[1] }) => api.credentials.update(id, data),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["credentials"] }); notify.success("Credential aktualisiert"); closeModal(); },
    onError: () => notify.error("Fehler beim Aktualisieren"),
  });

  const deleteMut = useMutation({
    mutationFn: (id: string) => api.credentials.delete(id),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["credentials"] }); notify.success("Credential geloescht"); setDeleteConfirm(null); },
    onError: () => notify.error("Fehler beim Loeschen"),
  });

  const openCreate = () => {
    setName(""); setCredType("login"); setUsername(""); setPassword("");
    setToken(""); setContent(""); setUrl(""); setNotes(""); setShowPassword(false);
    setModal({ open: true, editing: null });
  };

  const openEdit = (c: Credential) => {
    setName(c.name);
    setCredType(c.credential_type);
    setUsername(c.data_masked.username ?? "");
    setPassword(""); // never prefill password
    setToken("");
    setContent("");
    setUrl(c.url ?? "");
    setNotes(c.notes ?? "");
    setShowPassword(false);
    setModal({ open: true, editing: c });
  };

  const closeModal = () => setModal({ open: false, editing: null });

  const buildData = (): Record<string, string> => {
    if (credType === "login") return { username, password };
    if (credType === "token") return { token };
    return { content };
  };

  const handleSubmit = () => {
    if (!name.trim()) return;
    const data = buildData();
    if (modal.editing) {
      const payload: Parameters<typeof api.credentials.update>[1] = { name: name.trim() };
      if (credType !== modal.editing.credential_type) payload.credential_type = credType;
      // Only send data if user entered new values
      const hasNewData = credType === "login" ? password.trim() : credType === "token" ? token.trim() : content.trim();
      if (hasNewData) payload.data = data;
      if (url !== (modal.editing.url ?? "")) payload.url = url || undefined;
      if (notes !== (modal.editing.notes ?? "")) payload.notes = notes || undefined;
      updateMut.mutate({ id: modal.editing.id, data: payload });
    } else {
      createMut.mutate({ name: name.trim(), credential_type: credType, data, url: url || undefined, notes: notes || undefined });
    }
  };

  const isSubmitDisabled = () => {
    if (!name.trim()) return true;
    if (!modal.editing) {
      // Create: require data
      if (credType === "login") return !username.trim() || !password.trim();
      if (credType === "token") return !token.trim();
      return !content.trim();
    }
    return false; // Edit: name is enough (data optional)
  };

  return (
    <div className="flex flex-col gap-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-sm font-semibold" style={{ color: C.textPrimary }}>Credentials Vault</h3>
          <p className="text-[11px] mt-0.5" style={{ color: C.textMuted }}>
            Verschluesselte Zugangsdaten fuer Agent-Tasks
          </p>
        </div>
        <button
          onClick={openCreate}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-[11px] font-medium cursor-pointer transition-all"
          style={{ color: C.accent, border: `1px solid ${C.borderAccent}`, backgroundColor: C.accentSubtle }}
        >
          <Plus size={12} />
          Neu
        </button>
      </div>

      {/* List */}
      {isLoading ? (
        <div className="text-[11px] py-8 text-center" style={{ color: C.textMuted }}>Laden...</div>
      ) : !credentials?.length ? (
        <div className="text-[11px] py-8 text-center" style={{ color: C.textMuted }}>
          Noch keine Credentials gespeichert
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          {credentials.map((c) => {
            const cfg = TYPE_CONFIG[c.credential_type] ?? TYPE_CONFIG.custom;
            const Icon = cfg.icon;
            return (
              <div
                key={c.id}
                className="flex items-center gap-3 px-4 py-3 rounded-xl"
                style={{ backgroundColor: C.bgDeep, border: `1px solid ${C.border}` }}
              >
                <Icon size={14} style={{ color: cfg.color }} />
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="text-[12px] font-medium truncate" style={{ color: C.textPrimary }}>{c.name}</span>
                    <span className="text-[9px] px-1.5 py-0.5 rounded-full" style={{ color: cfg.color, border: `1px solid ${cfg.color}33`, backgroundColor: `${cfg.color}11` }}>
                      {cfg.label}
                    </span>
                  </div>
                  <div className="text-[10px] mt-0.5 truncate" style={{ color: C.textMuted }}>
                    {c.credential_type === "login" && c.data_masked.username ? `${c.data_masked.username} · ${c.data_masked.password}` : ""}
                    {c.credential_type === "token" ? c.data_masked.token : ""}
                    {c.credential_type === "custom" ? `(Freitext, ${c.data_masked.content?.length ?? 0} Zeichen)` : ""}
                    {c.url ? ` · ${c.url}` : ""}
                  </div>
                </div>
                <div className="flex items-center gap-1">
                  <button onClick={() => openEdit(c)} className="p-1.5 rounded-lg cursor-pointer hover:bg-white/5 transition-colors" aria-label="Credential bearbeiten">
                    <Pencil size={12} style={{ color: C.textMuted }} />
                  </button>
                  {deleteConfirm === c.id ? (
                    <button onClick={() => deleteMut.mutate(c.id)} className="px-2 py-1 rounded-lg text-[10px] font-medium cursor-pointer" style={{ color: C.error, backgroundColor: `${C.error}22` }}>
                      Wirklich?
                    </button>
                  ) : (
                    <button onClick={() => setDeleteConfirm(c.id)} className="p-1.5 rounded-lg cursor-pointer hover:bg-white/5 transition-colors" aria-label="Credential löschen">
                      <Trash2 size={12} style={{ color: C.textMuted }} />
                    </button>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* Create/Edit Modal */}
      <AnimatePresence>
        {modal.open && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 z-50 flex items-center justify-center p-4"
            onClick={(e) => { if (e.target === e.currentTarget) closeModal(); }}
          >
            <div className="absolute inset-0" style={{ backgroundColor: "rgba(0,0,0,0.6)" }} />
            <motion.div
              initial={{ opacity: 0, scale: 0.95, y: 10 }}
              animate={{ opacity: 1, scale: 1, y: 0 }}
              exit={{ opacity: 0, scale: 0.95, y: 10 }}
              role="dialog"
              aria-modal="true"
              aria-label={modal.editing ? "Credential bearbeiten" : "Neues Credential"}
              className="relative w-full max-w-md rounded-2xl overflow-hidden"
              style={{ background: C.bgElevated, border: `1px solid ${C.border}`, boxShadow: "0 4px 24px rgba(0,0,0,0.5), 0 1px 2px rgba(0,0,0,0.3)" }}
            >
              <div className="flex items-center justify-between px-5 py-3.5" style={{ borderBottom: `1px solid ${C.borderSubtle}` }}>
                <span className="text-sm font-semibold" style={{ color: C.textPrimary }}>
                  {modal.editing ? "Credential bearbeiten" : "Neues Credential"}
                </span>
                <button onClick={closeModal} className="cursor-pointer hover:opacity-80" style={{ color: C.textMuted }} aria-label="Modal schließen"><X size={16} /></button>
              </div>

              <div className="p-5 flex flex-col gap-3">
                {/* Name */}
                <label className="flex flex-col gap-1">
                  <span className="text-[10px]" style={{ color: C.textMuted }}>Name</span>
                  <input
                    autoFocus
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    placeholder="z.B. Vercel Account"
                    className="w-full text-[12px] px-3 py-2 rounded-xl outline-none"
                    style={{ border: `1px solid ${C.border}`, color: C.textPrimary, backgroundColor: C.bgDeep }}
                  />
                </label>

                {/* Type selector */}
                <div className="flex items-center gap-2">
                  <span className="text-[10px] shrink-0" style={{ color: C.textMuted }}>Typ:</span>
                  {(["login", "token", "custom"] as CredentialType[]).map((t) => {
                    const cfg = TYPE_CONFIG[t];
                    return (
                      <button
                        key={t}
                        type="button"
                        onClick={() => setCredType(t)}
                        className="px-2.5 py-1 text-[11px] font-medium rounded-full cursor-pointer transition-all"
                        style={{
                          backgroundColor: credType === t ? `${cfg.color}22` : "transparent",
                          color: credType === t ? cfg.color : C.textMuted,
                          border: `1px solid ${credType === t ? `${cfg.color}66` : C.border}`,
                        }}
                      >
                        {cfg.label}
                      </button>
                    );
                  })}
                </div>

                {/* Dynamic fields */}
                {credType === "login" && (
                  <>
                    <label className="flex flex-col gap-1">
                      <span className="text-[10px]" style={{ color: C.textMuted }}>Username</span>
                      <input
                        value={username}
                        onChange={(e) => setUsername(e.target.value)}
                        placeholder="Username"
                        className="w-full text-[12px] px-3 py-2 rounded-xl outline-none"
                        style={{ border: `1px solid ${C.border}`, color: C.textPrimary, backgroundColor: C.bgDeep }}
                      />
                    </label>
                    <label className="flex flex-col gap-1">
                      <span className="text-[10px]" style={{ color: C.textMuted }}>Passwort</span>
                      <div className="relative">
                        <input
                          type={showPassword ? "text" : "password"}
                          value={password}
                          onChange={(e) => setPassword(e.target.value)}
                          placeholder={modal.editing ? "Neues Passwort (leer = beibehalten)" : "Passwort"}
                          className="w-full text-[12px] px-3 py-2 rounded-xl outline-none pr-10"
                          style={{ border: `1px solid ${C.border}`, color: C.textPrimary, backgroundColor: C.bgDeep }}
                        />
                        <button
                          type="button"
                          onClick={() => setShowPassword(!showPassword)}
                          className="absolute right-2 top-1/2 -translate-y-1/2 cursor-pointer"
                          style={{ color: C.textMuted }}
                          aria-label={showPassword ? "Passwort verbergen" : "Passwort anzeigen"}
                        >
                          {showPassword ? <EyeOff size={14} /> : <Eye size={14} />}
                        </button>
                      </div>
                    </label>
                  </>
                )}

                {credType === "token" && (
                  <label className="flex flex-col gap-1">
                    <span className="text-[10px]" style={{ color: C.textMuted }}>Token / API Key</span>
                    <div className="relative">
                      <input
                        type={showPassword ? "text" : "password"}
                        value={token}
                        onChange={(e) => setToken(e.target.value)}
                        placeholder={modal.editing ? "Neuer Token (leer = beibehalten)" : "Token / API Key"}
                        className="w-full text-[12px] px-3 py-2 rounded-xl outline-none pr-10"
                        style={{ border: `1px solid ${C.border}`, color: C.textPrimary, backgroundColor: C.bgDeep }}
                      />
                      <button
                        type="button"
                        onClick={() => setShowPassword(!showPassword)}
                        className="absolute right-2 top-1/2 -translate-y-1/2 cursor-pointer"
                        style={{ color: C.textMuted }}
                        aria-label={showPassword ? "Token verbergen" : "Token anzeigen"}
                      >
                        {showPassword ? <EyeOff size={14} /> : <Eye size={14} />}
                      </button>
                    </div>
                  </label>
                )}

                {credType === "custom" && (
                  <label className="flex flex-col gap-1">
                    <span className="text-[10px]" style={{ color: C.textMuted }}>Inhalt</span>
                    <textarea
                      value={content}
                      onChange={(e) => setContent(e.target.value)}
                      placeholder={modal.editing ? "Neuer Inhalt (leer = beibehalten)" : "SSH Key, Connection String, etc."}
                      rows={4}
                      className="w-full text-[12px] px-3 py-2 rounded-xl outline-none resize-none font-mono"
                      style={{ border: `1px solid ${C.border}`, color: C.textPrimary, backgroundColor: C.bgDeep }}
                    />
                  </label>
                )}

                {/* URL */}
                <label className="flex flex-col gap-1">
                  <span className="text-[10px]" style={{ color: C.textMuted }}>URL (optional)</span>
                  <input
                    value={url}
                    onChange={(e) => setUrl(e.target.value)}
                    placeholder="https://..."
                    className="w-full text-[12px] px-3 py-2 rounded-xl outline-none"
                    style={{ border: `1px solid ${C.border}`, color: C.textPrimary, backgroundColor: C.bgDeep }}
                  />
                </label>

                {/* Notes */}
                <label className="flex flex-col gap-1">
                  <span className="text-[10px]" style={{ color: C.textMuted }}>Notizen (nicht verschlüsselt)</span>
                  <input
                    value={notes}
                    onChange={(e) => setNotes(e.target.value)}
                    placeholder="Notizen (optional)"
                    className="w-full text-[12px] px-3 py-2 rounded-xl outline-none"
                    style={{ border: `1px solid ${C.border}`, color: C.textMuted, backgroundColor: C.bgDeep }}
                  />
                </label>
              </div>

              <div className="flex items-center justify-end gap-2 px-5 py-3.5" style={{ borderTop: `1px solid ${C.borderSubtle}` }}>
                <button onClick={closeModal} className="px-3.5 py-1.5 text-[11px] rounded-lg cursor-pointer" style={{ color: C.textMuted, border: `1px solid ${C.border}` }}>
                  Abbrechen
                </button>
                <button
                  onClick={handleSubmit}
                  disabled={isSubmitDisabled() || createMut.isPending || updateMut.isPending}
                  className="px-3.5 py-1.5 text-[11px] font-semibold rounded-lg cursor-pointer transition-all disabled:opacity-30"
                  style={{ background: `linear-gradient(135deg, ${C.accent}, ${C.accentHover})`, color: "#fff" }}
                >
                  {createMut.isPending || updateMut.isPending ? "..." : modal.editing ? "Speichern" : "Erstellen"}
                </button>
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
