"use client";

import { useEffect, useState, useRef } from "react";
import { useRouter } from "next/navigation";
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
        setError("Name ist erforderlich.");
        return;
      }
      if (password.length < 6) {
        setError("Passwort muss mindestens 6 Zeichen lang sein.");
        return;
      }
      if (password !== confirmPassword) {
        setError("Passwoerter stimmen nicht ueberein.");
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
        err instanceof Error ? err.message : "Verbindung fehlgeschlagen.";
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
          className="w-5 h-5 rounded-full border-2 border-t-transparent animate-spin"
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
    "w-full bg-transparent border rounded-lg px-3 py-2.5 text-sm outline-none transition-all duration-200";

  return (
    <main
      className="min-h-dvh flex items-center justify-center relative"
      style={{ backgroundColor: "var(--color-bg-deep)" }}
    >
      <AmbientBackground />

      <motion.div
        initial={{ opacity: 0, y: 16 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.5, ease: [0.16, 1, 0.3, 1] }}
        className="w-full max-w-sm px-4 relative z-10"
      >
        {/* Logo / Title */}
        <div className="text-center mb-8">
          <motion.div
            initial={{ scale: 0.8, opacity: 0 }}
            animate={{ scale: 1, opacity: 1 }}
            transition={{ delay: 0.1, duration: 0.4, ease: [0.16, 1, 0.3, 1] }}
            className="mb-4"
            style={{ height: 0 }}
          />
          <h1
            style={{
              color: "var(--color-text-primary)",
              fontFamily: "var(--font-wordmark), ui-sans-serif, system-ui",
              fontWeight: 500,
              fontSize: "34px",
              letterSpacing: "-0.04em",
              lineHeight: 1,
            }}
          >
            {BRAND_MAIN}<span style={{ color: C.accent }}>{BRAND_ACCENT}</span>
          </h1>
          <p
            className="text-sm mt-1.5"
            style={{ color: "var(--color-text-secondary)" }}
          >
            {isRegister
              ? "Ersten Admin-Account erstellen"
              : "Anmelden"}
          </p>
        </div>

        {/* Card */}
        <motion.form
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.15, duration: 0.4, ease: [0.16, 1, 0.3, 1] }}
          onSubmit={handleSubmit}
          className="p-6 space-y-4 rounded-2xl"
          style={{ background: C.bgSurface, border: `1px solid ${C.border}`, borderRadius: 12 }}
        >
          {/* Name (register only) */}
          {isRegister && (
            <div className="space-y-1.5">
              <label
                htmlFor="name"
                className="text-nav"
              >
                Name
              </label>
              <input
                ref={nameRef}
                id="name"
                type="text"
                autoComplete="name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Dein Name"
                className={inputClasses}
                style={{
                  backgroundColor: "rgba(255, 255, 255, 0.03)",
                  borderColor: "var(--color-border)",
                  color: "var(--color-text-primary)",
                }}
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
            <label
              htmlFor="email"
              className="text-nav"
            >
              E-Mail
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
              style={{
                backgroundColor: "rgba(255, 255, 255, 0.03)",
                borderColor: "var(--color-border)",
                color: "var(--color-text-primary)",
              }}
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
            <label
              htmlFor="password"
              className="text-nav"
            >
              Passwort
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
                placeholder={isRegister ? "Min. 6 Zeichen" : "Passwort"}
                className={`${inputClasses} pr-10 font-mono`}
                style={{
                  backgroundColor: "rgba(255, 255, 255, 0.03)",
                  borderColor: "var(--color-border)",
                  color: "var(--color-text-primary)",
                }}
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
                aria-label={showPassword ? "Passwort verbergen" : "Passwort anzeigen"}
              >
                {showPassword ? <EyeOff size={15} /> : <Eye size={15} />}
              </button>
            </div>
          </div>

          {/* Confirm Password (register only) */}
          {isRegister && (
            <div className="space-y-1.5">
              <label
                htmlFor="confirm"
                className="text-nav"
              >
                Passwort wiederholen
              </label>
              <input
                id="confirm"
                type="password"
                autoComplete="new-password"
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                placeholder="Nochmal eingeben"
                className={`${inputClasses} font-mono`}
                style={{
                  backgroundColor: "rgba(255, 255, 255, 0.03)",
                  borderColor: "var(--color-border)",
                  color: "var(--color-text-primary)",
                }}
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
              className="text-xs rounded-lg px-3 py-2"
              style={{
                color: "var(--color-error)",
                backgroundColor: "rgba(239, 68, 68, 0.08)",
                border: "1px solid rgba(239, 68, 68, 0.15)",
              }}
            >
              {error}
            </motion.p>
          )}

          {/* Submit button */}
          <button
            type="submit"
            disabled={loading || !email.trim() || !password.trim()}
            className="w-full text-white font-medium text-sm rounded-lg px-4 py-2.5 flex items-center justify-center gap-2 cursor-pointer transition-all duration-200 disabled:opacity-40 disabled:cursor-not-allowed"
            style={{
              background: `linear-gradient(135deg, ${C.accent}, ${C.accentHover})`,
            }}
          >
            {loading && <Loader2 className="animate-spin" size={14} />}
            {loading
              ? "Wird geprueft..."
              : isRegister
                ? "Admin erstellen"
                : "Einloggen"}
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
            Erster User wird automatisch zum Admin.
          </motion.p>
        )}
      </motion.div>
    </main>
  );
}
