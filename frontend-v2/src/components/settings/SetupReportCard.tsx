"use client";

/**
 * "Copy setup report" — Settings → About (Welle 3, PR 3-4). One button that
 * copies a plain-text, local-only snapshot to the clipboard: version,
 * health, which provider keys test ok/fail, the onboarding timings
 * (login, first key green, first agent online, first question card) and the
 * last few error codes. Nothing here leaves the device by itself — it is
 * only ever copied on the operator's own click, to paste into a support
 * request. See lib/setupReport.ts for the privacy rule (no names, keys,
 * host names or e-mails ever enter the report).
 */
import { useState } from "react";
import { useTranslations } from "next-intl";
import { ClipboardCopy, Check } from "lucide-react";
import { notify } from "@/lib/notify";
import { buildSetupReport, type SetupReportLabels } from "@/lib/setupReport";
import { C } from "@/lib/colors";

const cardStyle = {
  background: C.bgSurface,
  border: `1px solid ${C.border}`,
  borderRadius: 12,
} as const;

export function SetupReportCard() {
  const t = useTranslations("settings.about.setupReport");
  const [copying, setCopying] = useState(false);
  const [copied, setCopied] = useState(false);

  async function copyReport() {
    setCopying(true);
    try {
      const labels: SetupReportLabels = {
        heading: t("heading"),
        version: t("version"),
        unknown: t("unknown"),
        health: t("health"),
        database: t("database"),
        redis: t("redis"),
        agentsOnline: t("agentsOnline"),
        keysHeading: t("keysHeading"),
        noKeys: t("noKeys"),
        keyState: {
          ok: t("keyStateOk"),
          fail: t("keyStateFail"),
          never_tested: t("keyStateNeverTested"),
        },
        timesHeading: t("timesHeading"),
        milestone: {
          login: t("milestoneLogin"),
          first_key_green: t("milestoneFirstKeyGreen"),
          first_agent_online: t("milestoneFirstAgentOnline"),
          first_question_card: t("milestoneFirstQuestionCard"),
        },
        notYet: t("notYet"),
        errorsHeading: t("errorsHeading"),
        noErrors: t("noErrors"),
      };
      const report = await buildSetupReport(labels);
      await navigator.clipboard.writeText(report);
      notify.success(t("copied"));
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      notify.error(t("copyFailed"));
    } finally {
      setCopying(false);
    }
  }

  return (
    <div className="mc-card p-6" style={cardStyle}>
      <h3 className="text-sm font-semibold mb-1" style={{ color: "var(--color-text-primary)" }}>
        {t("title")}
      </h3>
      <p className="text-sm mb-4" style={{ color: "var(--color-text-secondary)" }}>
        {t("description")}
      </p>
      <button
        type="button"
        onClick={copyReport}
        disabled={copying}
        className="flex items-center gap-2 text-sm font-medium rounded-sm px-3 py-2 cursor-pointer transition-all duration-200 disabled:opacity-50 disabled:cursor-not-allowed"
        style={{
          background: "var(--color-bg-deep)",
          border: `1px solid ${C.border}`,
          color: "var(--color-text-primary)",
        }}
      >
        {copied ? <Check size={14} style={{ color: C.online }} /> : <ClipboardCopy size={14} />}
        {t("copyButton")}
      </button>
    </div>
  );
}
