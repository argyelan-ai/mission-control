"use client";

/**
 * AddDeviceDialog — „Gerät hinzufügen": EIN Einstieg statt vier Knöpfe.
 *
 * Vorher standen in der Geräteverwaltung vier Wege nebeneinander („Box
 * hinzufügen" · „Host" · „Gerät automatisch einrichten" · „Gerät meldet sich
 * selbst"), und man sah ihnen nicht an, wann man welchen nimmt. Dieser Dialog
 * fragt nicht nach dem Verfahren, sondern nach der SITUATION des Operators —
 * und sagt je Wahl in einem Satz, was danach passiert:
 *
 *   onboard  „Ich habe Benutzername und Passwort"    → HostOnboardDialog
 *   pairing  „Ich kann selbst auf der Box arbeiten" → NodePairingDialog
 *   wizard   „MC erreicht die Box schon per Schlüssel" → BoxWizard (mit Modell)
 *   manual   Nebenweg: Formular von Hand (flask_wol / local haben NUR diesen)
 *
 * Der Dialog entscheidet nichts und ruft keine API: er liefert nur die
 * gewählte Route (`onChoose`) — das Öffnen des passenden Dialogs bleibt in
 * HostsSection. So kann der Betreiber später die Modell-Schritte an die
 * Passwort-Route anhängen (Onboarding → Wizard-Schritt 3), ohne hier etwas
 * umzubauen: nur der Handler für `onboard` in HostsSection ändert sich.
 *
 * Alle vier Wege bleiben erreichbar; nichts wurde entfernt.
 */

import { useTranslations } from "next-intl";
import { ChevronRight, KeyRound, Radio, Terminal, X } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { ResponsiveModal } from "@/components/shared/ResponsiveModal";
import { C } from "@/lib/colors";

export type DeviceRoute = "onboard" | "pairing" | "wizard" | "manual";

/** Reihenfolge = empfohlene Reihenfolge. Der Weg mit den geringsten
 *  Voraussetzungen (nur Zugangsdaten) steht zuoberst. */
const ROUTES: ReadonlyArray<{
  route: Exclude<DeviceRoute, "manual">;
  icon: LucideIcon;
  titleKey: string;
  descKey: string;
  recommended?: boolean;
}> = [
  { route: "onboard", icon: KeyRound, titleKey: "routeOnboardTitle", descKey: "routeOnboardDesc", recommended: true },
  { route: "pairing", icon: Terminal, titleKey: "routePairingTitle", descKey: "routePairingDesc" },
  { route: "wizard", icon: Radio, titleKey: "routeWizardTitle", descKey: "routeWizardDesc" },
];

export function AddDeviceDialog({
  open,
  onClose,
  onChoose,
}: {
  open: boolean;
  onClose: () => void;
  onChoose: (route: DeviceRoute) => void;
}) {
  const t = useTranslations("runtimes.hosts");

  return (
    <ResponsiveModal open={open} onClose={onClose} aria-labelledby="add-device-title" className="sm:max-w-md">
      <div
        className="flex items-start justify-between p-5 border-b shrink-0"
        style={{ borderColor: "var(--color-border)" }}
      >
        <div className="flex flex-col gap-1">
          <h2 id="add-device-title" className="text-sm font-semibold" style={{ color: C.textPrimary }}>
            {t("addDeviceTitle")}
          </h2>
          <p className="text-xs" style={{ color: C.textMuted }}>{t("addDeviceQuestion")}</p>
        </div>
        <button
          onClick={onClose}
          aria-label={t("close")}
          className="p-1 rounded-md hover:bg-[var(--color-bg-hover)] cursor-pointer"
        >
          <X size={14} style={{ color: C.textMuted }} />
        </button>
      </div>

      {/* Situationen — eine Liste, keine drei gleichen Karten nebeneinander.
          Die Zeilen tragen eine Mono-Laufnummer als Instrumentenstimme; der
          empfohlene Weg hebt sich über Fläche + Rahmen ab, nicht über Farbe. */}
      <div className="flex-1 overflow-y-auto p-5 flex flex-col gap-2" role="group" aria-label={t("addDeviceQuestion")}>
        {ROUTES.map(({ route, icon: Icon, titleKey, descKey, recommended }, i) => (
          <button
            key={route}
            type="button"
            data-testid={`add-device-${route}`}
            onClick={() => onChoose(route)}
            className="group flex items-start gap-3 w-full text-left px-4 py-3 rounded-lg cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)]"
            style={{
              background: recommended ? C.accentSubtle : "transparent",
              border: `1px solid ${recommended ? C.borderAccent : C.border}`,
            }}
          >
            <span className="label-sys tabular-nums pt-1 shrink-0" aria-hidden>
              {String(i + 1).padStart(2, "0")}
            </span>
            <Icon size={14} className="shrink-0 mt-1" style={{ color: recommended ? C.accent : C.textMuted }} />
            <span className="flex flex-col gap-0.5 min-w-0 flex-1">
              <span className="flex items-center gap-2">
                <span className="text-sm font-medium" style={{ color: C.textPrimary }}>{t(titleKey)}</span>
                {recommended && <span className="label-sys label-sys--accent">{t("routeRecommended")}</span>}
              </span>
              <span className="text-xs leading-relaxed" style={{ color: C.textSecondary }}>{t(descKey)}</span>
            </span>
            <ChevronRight
              size={14}
              className="shrink-0 mt-1 transition-transform group-hover:translate-x-0.5"
              style={{ color: C.textMuted }}
            />
          </button>
        ))}

        {/* Nebenweg: Sonderfälle ohne SSH (Windows-Box mit Wake-on-LAN, dieser
            Rechner selbst) haben NUR das Formular. Bewusst unauffällig — er
            darf den Hauptfall nicht verkomplizieren. */}
        <p className="text-xs pt-3 mt-1 border-t leading-relaxed" style={{ color: C.textMuted, borderColor: C.borderSubtle }}>
          {t("routeManualLead")}{" "}
          <button
            type="button"
            data-testid="add-device-manual"
            onClick={() => onChoose("manual")}
            className="underline underline-offset-2 cursor-pointer hover:text-[var(--color-text-primary)]"
            style={{ color: C.textSecondary }}
          >
            {t("routeManualButton")}
          </button>
        </p>
      </div>
    </ResponsiveModal>
  );
}
