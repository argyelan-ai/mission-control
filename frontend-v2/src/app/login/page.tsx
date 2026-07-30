"use client";

import { useEffect, useState, useRef } from "react";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { motion } from "framer-motion";
import { Loader2, Eye, EyeOff } from "lucide-react";
import { AUTH_TOKEN_KEY, api, setStoredUser } from "@/lib/api";
import { AmbientBackground } from "@/components/layout/AmbientBackground";
import { C } from "@/lib/colors";

const _BRAND = process.env.NEXT_PUBLIC_BRAND || "Mission.Control";
const _dot = _BRAND.lastIndexOf(".");
const BRAND_MAIN = _dot > 0 ? _BRAND.slice(0, _dot) : _BRAND;
const BRAND_ACCENT = _dot > 0 ? _BRAND.slice(_dot) : "";

type Mode = "loading" | "login" | "register";

export default function LoginPage() {
  const t = useTranslations("login");
  const router = useRouter();
  const [mode, setMode] = useState<Mode>("loading");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [name, setName] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [showPassword, setShowPassword] = useState(false);
  const emailRef = useRef<HTMLInputElement>(null);
  const nameRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    api.auth
      .setupRequired()
      .then((res) => {
        setMode(res.setup_required ? "register" : "login");
      })
      .catch(() => {
        setMode("login");
      });
  }, []);

  // Auto-focus after mode resolves
  useEffect(() => {
    if (mode === "register") {
      nameRef.current?.focus();
    } else if (mode === "login") {
      emailRef.current?.focus();
    }
  }, [mode]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!email.trim() || !password.trim()) return;

    if (mode === "register") {
      if (!name.trim()) {
        setError(t("nameRequired"));
        return;
      }
      if (password.length < 6) {
        setError(t("passwordMin"));
        return;
      }
      if (password !== confirmPassword) {
        setError(t("passwordsMismatch"));
        return;
      }
    }

    setLoading(true);
    setError("");

    try {
      const res =
        mode === "register"
          ? await api.auth.register(email.trim(), name.trim(), password)
          : await api.auth.login(email.trim(), password);

      localStorage.setItem(AUTH_TOKEN_KEY, res.access_token);
      setStoredUser(res.user);
      // Erst-Registrierung -> First-Run-Wizard (Provider-Key, Startinhalte)
      router.replace(mode === "register" ? "/setup" : "/");
    } catch (err) {
      const msg =
        err instanceof Error ? err.message : t("connectionFailed");
      setError(msg.replace(/^"/, "").replace(/"$/, ""));
      setLoading(false);
    }
  }

  if (mode === "loading") {
    return (
      <div
        className="min-h-dvh flex items-center justify-center"
        style={{ backgroundColor: "var(--color-bg-deep)" }}
      >
        <AmbientBackground />
        <div
          className="w-5 h-5 border-2 border-t-transparent animate-spin"
          style={{
            borderColor: "var(--color-accent)",
            borderTopColor: "transparent",
          }}
        />
      </div>
    );
  }

  const isRegister = mode === "register";

  const inputClasses =
    "w-full bg-transparent border rounded-sm px-3 py-2.5 text-sm outline-none transition-all duration-200";

  const inputStyle = {
    backgroundColor: "var(--color-bg-deep)",
    borderColor: "var(--color-border)",
    color: "var(--color-text-primary)",
  } as const;

  return (
    <main
      className="min-h-dvh relative flex"
      style={{ backgroundColor: "var(--color-bg-deep)" }}
    >
      <AmbientBackground />

      {/* ── Brand-Zone (links, Desktop) — asymmetrische Bühne ──────────── */}
      <div className="hidden md:flex flex-col justify-between flex-1 p-10 lg:p-14 relative z-10">
        <motion.div
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5, ease: [0.16, 1, 0.3, 1] }}
        >
          <div className="label-sys label-sys--accent">{t("consoleAccess")}</div>
          <h1
            className="display mt-4"
            style={{
              color: "var(--color-text-primary)",
              fontWeight: 600,
              fontSize: "clamp(2.75rem, 5vw, 4.5rem)",
              lineHeight: 0.95,
              letterSpacing: "-0.03em",
            }}
          >
            {BRAND_MAIN}
            <span style={{ color: C.accent }}>{BRAND_ACCENT}</span>
          </h1>
          <p
            className="mt-5 max-w-sm text-[15px] leading-relaxed"
            style={{ color: "var(--color-text-secondary)" }}
          >
            {t("tagline")}
          </p>
        </motion.div>

        {/* Koordinaten-Fusszeile — Mono-Instrumenten-Detail */}
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ delay: 0.4, duration: 0.5 }}
          className="flex items-center gap-6 font-mono uppercase"
          style={{
            color: "var(--color-text-dim)",
            fontSize: "10px",
            letterSpacing: "0.14em",
          }}
        >
          <span>47.3769° N · 8.5417° E</span>
          <span aria-hidden style={{ color: "var(--color-border-accent)" }}>
            /
          </span>
          <span>Fleet · Live</span>
          <span aria-hidden style={{ color: "var(--color-border-accent)" }}>
            /
          </span>
          <span>v3</span>
        </motion.div>
      </div>

      {/* ── Form-Zone ───────────────────────────────────────────────────── */}
      <div className="flex flex-1 md:max-w-[480px] items-center justify-center px-4 py-10 relative z-10">
        <motion.div
          initial={{ opacity: 0, y: 16 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5, ease: [0.16, 1, 0.3, 1] }}
          className="w-full max-w-sm"
        >
          {/* Mobile-Wordmark (Desktop hat die Brand-Zone links) */}
          <div className="md:hidden mb-8">
            <div className="label-sys label-sys--accent mb-3">{t("consoleAccess")}</div>
            <h1
              className="display"
              style={{
                color: "var(--color-text-primary)",
                fontWeight: 600,
                fontSize: "34px",
                letterSpacing: "-0.03em",
                lineHeight: 1,
              }}
            >
              {BRAND_MAIN}
              <span style={{ color: C.accent }}>{BRAND_ACCENT}</span>
            </h1>
          </div>

          <p className="label-sys mb-4">
            {isRegister ? t("createFirstAdmin") : t("signIn")}
          </p>

          {/* Card */}
          <motion.form
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.15, duration: 0.4, ease: [0.16, 1, 0.3, 1] }}
            onSubmit={handleSubmit}
            className="p-6 space-y-4 rounded-md corner-ticks"
            style={{
              background: "var(--color-bg-surface)",
              border: "1px solid var(--color-border)",
            }}
          >
            {/* Name (register only) */}
            {isRegister && (
              <div className="space-y-1.5">
                <label htmlFor="name" className="label-sys">
                  {t("name")}
                </label>
                <input
                  ref={nameRef}
                  id="name"
                  type="text"
                  autoComplete="name"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder={t("yourName")}
                  className={inputClasses}
                  style={inputStyle}
                  onFocus={(e) =>
                    (e.currentTarget.style.borderColor = "var(--color-accent)")
                  }
                  onBlur={(e) =>
                    (e.currentTarget.style.borderColor = "var(--color-border)")
                  }
                />
              </div>
            )}

            {/* Email */}
            <div className="space-y-1.5">
              <label htmlFor="email" className="label-sys">
                {t("email")}
              </label>
              <input
                ref={emailRef}
                id="email"
                type="email"
                autoComplete="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="admin@example.com"
                className={inputClasses}
                style={inputStyle}
                onFocus={(e) =>
                  (e.currentTarget.style.borderColor = "var(--color-accent)")
                }
                onBlur={(e) =>
                  (e.currentTarget.style.borderColor = "var(--color-border)")
                }
              />
            </div>

            {/* Password */}
            <div className="space-y-1.5">
              <label htmlFor="password" className="label-sys">
                {t("password")}
              </label>
              <div className="relative">
                <input
                  id="password"
                  type={showPassword ? "text" : "password"}
                  autoComplete={
                    isRegister ? "new-password" : "current-password"
                  }
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder={isRegister ? t("min6Chars") : t("password")}
                  className={`${inputClasses} pr-10 font-mono`}
                  style={inputStyle}
                  onFocus={(e) =>
                    (e.currentTarget.style.borderColor = "var(--color-accent)")
                  }
                  onBlur={(e) =>
                    (e.currentTarget.style.borderColor = "var(--color-border)")
                  }
                />
                <button
                  type="button"
                  onClick={() => setShowPassword(!showPassword)}
                  className="absolute right-3 top-1/2 -translate-y-1/2 cursor-pointer"
                  style={{ color: "var(--color-text-muted)" }}
                  tabIndex={-1}
                  aria-label={showPassword ? t("hidePassword") : t("showPassword")}
                >
                  {showPassword ? <EyeOff size={15} /> : <Eye size={15} />}
                </button>
              </div>
            </div>

            {/* Confirm Password (register only) */}
            {isRegister && (
              <div className="space-y-1.5">
                <label htmlFor="confirm" className="label-sys">
                  {t("repeatPassword")}
                </label>
                <input
                  id="confirm"
                  type="password"
                  autoComplete="new-password"
                  value={confirmPassword}
                  onChange={(e) => setConfirmPassword(e.target.value)}
                  placeholder={t("repeatPassword")}
                  className={`${inputClasses} font-mono`}
                  style={inputStyle}
                  onFocus={(e) =>
                    (e.currentTarget.style.borderColor = "var(--color-accent)")
                  }
                  onBlur={(e) =>
                    (e.currentTarget.style.borderColor = "var(--color-border)")
                  }
                />
              </div>
            )}

            {/* Error message */}
            {error && (
              <motion.p
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: "auto" }}
                className="text-xs rounded-sm px-3 py-2"
                style={{
                  color: "var(--color-status-error-text)",
                  backgroundColor: "rgba(194, 56, 56, 0.1)",
                  border: "1px solid rgba(194, 56, 56, 0.25)",
                }}
              >
                {error}
              </motion.p>
            )}

            {/* Submit button — Akzent-Fläche, dunkler Text (Kontrast!) */}
            <button
              type="submit"
              disabled={loading || !email.trim() || !password.trim()}
              className="w-full font-semibold text-sm rounded-sm px-4 py-2.5 flex items-center justify-center gap-2 cursor-pointer transition-all duration-200 disabled:opacity-40 disabled:cursor-not-allowed hover:brightness-110"
              style={{
                background: C.accent,
                color: C.onAccent,
              }}
            >
              {loading && <Loader2 className="animate-spin" size={14} />}
              {loading
                ? t("signingIn")
                : isRegister
                  ? t("createAdmin")
                  : t("signIn")}
            </button>
          </motion.form>

          {isRegister && (
            <motion.p
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              transition={{ delay: 0.3 }}
              className="text-center text-xs mt-4"
              style={{ color: "var(--color-text-muted)" }}
            >
              {t("firstUserAdmin")}
            </motion.p>
          )}
        </motion.div>
      </div>
    </main>
  );
}
