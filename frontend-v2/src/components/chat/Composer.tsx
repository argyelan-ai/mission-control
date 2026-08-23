"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { Brain, Square, ArrowUp, ChevronDown, Paperclip, X, FileText} from "lucide-react";
import { C, STATUS } from "@/lib/colors";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import {
  type ChatAttachment,
  extractErrorMessage,
  isAgentBusyError,
  isEffortSwitchFailedError,
  isEffortSwitchRejectedError,
  isInputNotSupportedError,
  isSessionOnlyEffort,
  type ChatCapabilities,
  type ChatModelOption,
  type ChatSlashCommand,
  type StateEvent,
  type UsageEvent,
} from "@/lib/chatTypes";
import { CLAUDE_MODELS, SLASH_COMMANDS, formatCompactTokens, type SlashCommand } from "@/lib/claudeCommands";
import { ContextPanel } from "./ContextPanel";

const MAX_ROWS = 8;
/** Rows of room the input holds at rest, before anything is typed. */
const MIN_ROWS = 2;
// Matches the textarea's own line-height (14px body × 1.5, rounded) so the
// auto-grow ceiling lands on a whole row instead of clipping one mid-glyph.
const LINE_HEIGHT_PX = 22;

// Desktop-app-style context ring — compact circular indicator instead of a
// bar, per the operator's ask to match the Claude Desktop app's look.
const RING_SIZE = 18;
const RING_STROKE = 2;
const RING_RADIUS = (RING_SIZE - RING_STROKE) / 2;
const RING_CIRCUMFERENCE = 2 * Math.PI * RING_RADIUS;

/**
 * The switchable levels, straight from the server's capability block — no
 * hardcoded list, because it differs per harness and per CLI version and the
 * backend is the same source that validates the switch.
 *
 * Returns `[]` when the agent can't switch at all (every host agent: no pane to
 * drive) or when the backend predates the field. An empty list means the chip
 * shows the level read-only instead of opening an empty picker.
 */
export function resolveEffortLevels(capabilities: ChatCapabilities | null | undefined): string[] {
  if (!capabilities?.canSwitchEffort) return [];
  return (capabilities.effortLevels ?? []).filter(
    (level): level is string => typeof level === "string" && level.trim().length > 0,
  );
}


/**
 * Warum der Regler fehlt, in einem Satz — aus dem `effortReason` des Backends.
 *
 * Der haeufigste Fall ist neu und war vorher unsichtbar: bei openclaude
 * haengen die Effort-Stufen am MODELL, nicht am Harness. Ein Agent auf einem
 * eigenen/lokalen Modell bekommt darum keinen Regler, obwohl seine CLI
 * `/effort` sehr wohl kennt. Ohne diesen Satz sieht man nur, dass etwas
 * fehlt — und sucht den Fehler bei sich.
 */
export function effortReasonText(
  capabilities: ChatCapabilities | null | undefined,
  model: string | null | undefined,
  t: (key: string, values?: Record<string, string>) => string,
): string {
  /* Das Modell, ueber das die CLI die Aussage gemacht hat, schlaegt das gerade
     angezeigte: die Effort-Messung ist zwischengespeichert, das Modell-Label
     nicht. Ohne diesen Vorrang benennt der Satz nach einem Modellwechsel das
     FRISCHE Modell und behauptet ueber es etwas, das nur fuer das alte gemessen
     wurde. */
  const measured = capabilities?.effortModel ?? model;
  switch (capabilities?.effortReason) {
    case "model_no_effort":
      return measured
        ? t("effortLockedModelNoEffort", { model: measured })
        : t("effortLockedModelNoEffortGeneric");
    case "foreign_harness":
      return t("effortLockedForeignHarness");
    /* `no_pane` ist zugleich der Rueckfall: der Text nennt die fehlende
       Terminal-Steuerung und ist damit fuer aeltere Backends ohne Grund-Feld
       die einzige Aussage, die nicht raet. */
    case "no_pane":
    default:
      return t("effortLockedHint");
  }
}

/**
 * The palette's command list: everything the harness reports (built-ins plus
 * the agent's own skills), falling back to the short static list only while the
 * backend doesn't ship the field.
 *
 * Normalizes the leading slash instead of trusting it: the backend assembles
 * this from more than one source (CLI built-ins, skill directories), and a name
 * arriving as either `compact` or `/compact` must not produce `//compact` or a
 * row the "/"-prefix filter can never match. Entries without a usable name are
 * dropped rather than rendered as an unclickable blank.
 */
export function resolveSlashCommands(
  reported: ChatSlashCommand[] | null | undefined,
): SlashCommand[] {
  // null/undefined = aelteres Backend ohne das Feld -> statische Claude-Liste.
  // Ein EXPLIZIT leeres Array ist dagegen eine Aussage: dieser Harness (kimi,
  // omp) hat diese Kommandos nicht — dann darf die Palette nichts Falsches
  // versprechen (kritischer Test-Durchgang 18.08.2026).
  if (reported == null) return SLASH_COMMANDS;
  if (reported.length === 0) return [];
  const normalized = reported
    .filter((cmd): cmd is ChatSlashCommand => typeof cmd?.name === "string" && cmd.name.trim().length > 0)
    .map((cmd) => {
      const bare = cmd.name.trim().replace(/^\/+/, "");
      return { command: `/${bare}`, description: cmd.description?.trim() || "" };
    })
    .filter((cmd) => cmd.command.length > 1);
  return normalized;
}

/** One row of the model switcher, after normalization. */
export interface ModelChoice {
  /** Sent as `/model <name>`. */
  name: string;
  label: string;
  /** `null` = the harness didn't say; the row then shows no size suffix. */
  contextWindow: number | null;
}

/**
 * The model switcher's rows, from the harness when it reports them, otherwise
 * the static list without any window sizes.
 *
 * The frontend deliberately keeps no model→window map of its own: such a map is
 * wrong the day a new model ships, and the composer's context ring already
 * learned that lesson (`contextWindow` on the usage event is backend-stamped for
 * the same reason).
 */
export function resolveModelOptions(
  reported: ChatModelOption[] | null | undefined,
): ModelChoice[] {
  const fallback = CLAUDE_MODELS.map((m) => ({ name: m.name, label: m.label, contextWindow: null }));
  // Gleiche Unterscheidung wie bei den Slash-Kommandos: fehlendes Feld =
  // altes Backend -> Fallback; explizit leer = dieser Harness kennt unsere
  // /model-Aliasse nicht -> nichts anbieten.
  if (reported == null) return fallback;
  if (reported.length === 0) return [];
  const normalized = reported
    .map((option) => {
      const name = (option.command ?? option.name ?? "").trim();
      const window = typeof option.contextWindow === "number" && option.contextWindow > 0
        ? option.contextWindow
        : null;
      return { name, label: option.label?.trim() || name, contextWindow: window };
    })
    .filter((option) => option.name.length > 0);
  return normalized;
}

type RingThreshold = "normal" | "warning" | "error";

function ringThreshold(pct: number): RingThreshold {
  if (pct >= 90) return "error";
  if (pct >= 75) return "warning";
  return "normal";
}

function ringColor(threshold: RingThreshold): string {
  if (threshold === "error") return STATUS.error;
  if (threshold === "warning") return STATUS.warning;
  return C.textDim;
}

interface ComposerProps {
  agentId: string;
  /** Transcript truth — never an optimistic guess of what model is active. */
  usage: UsageEvent | null;
  state: StateEvent | null;
  onSend: (text: string) => void;
  onStop: () => void;
  /** Whether the underlying CLI session is currently live (derived from
   *  `session.aliveness`) — distinct from `state.status`. Gates the primary
   *  button entirely: an ended session can neither be sent to nor interrupted,
   *  so it gets no button at all rather than one that fails. Defaults to
   *  `true` so a caller that hasn't wired the real value yet keeps a usable
   *  composer instead of silently losing its only control. */
  sessionLive?: boolean;
  /** Kann MC den Bildschirm des Agenten lesen? Nur fuer `cli-bridge` (Docker+tmux)
   *  liefert `capture_pane` echten Text. Bei Host-Agenten (Boss, Hermes, Jarvis)
   *  gibt es diesen Kanal nicht: `_compute_pane_state` leitet dort `working`/`idle`
   *  allein aus der Transkript-mtime ab und ist sich dabei IMMER sicher — ein Boss,
   *  der denkt ohne zu schreiben, liest sich als `idle`. Genau dann fehlte bisher
   *  der Stop-Knopf, obwohl er der einzige noetige Griff ist. */
  paneObservable?: boolean;
  /** Server-derived harness capabilities. Drives the effort chip: no
   *  capabilities (or `canSwitchEffort: false`) means the level is shown
   *  read-only rather than as a picker that cannot work. */
  capabilities?: ChatCapabilities | null;
}

/**
 * The input row of the Sessions Chat view: auto-growing textarea, model
 * switcher + context meter, and a "/" command palette matching the
 * Codex/Claude-Desktop pattern: anchored directly above the input, live
 * prefix-filtered as you type, ArrowUp/Down + Enter/Tab/Escape to navigate.
 * Enter sends, Shift+Enter inserts a newline. Text reaches `onSend` raw —
 * CRLF normalization already happens in `api.chat.sendText` (B1), so this
 * never touches it twice.
 *
 * The palette is a plain filtered list, not cmdk — cmdk's own filtering
 * needs a `Command.Input` living inside the `Command` tree to drive its
 * internal search state, but the real input here is the message textarea
 * itself (typing continues in place after "/", exactly like Codex/Claude
 * Desktop), which structurally can't be that descendant. Without it, cmdk's
 * `search` state simply stayed empty forever and every item was shown
 * unfiltered — the bug this component was rewritten to fix.
 */
export function Composer({ agentId, usage, state, onSend, onStop, sessionLive = true, capabilities = null, paneObservable = true }: ComposerProps) {
  const t = useTranslations("sessions");
  const [text, setText] = useState("");
  const [focused, setFocused] = useState(false);
  const [modelOpen, setModelOpen] = useState(false);
  const modelBoxRef = useRef<HTMLDivElement | null>(null);
  const effortBoxRef = useRef<HTMLDivElement | null>(null);
  const [contextOpen, setContextOpen] = useState(false);
  const [effortOpen, setEffortOpen] = useState(false);
  /** Vorschau-Position des Reglers waehrend des Ziehens; `null` = zeige den
   *  echten aktuellen Wert. Erst das Loslassen schickt den Wechsel los. */
  const [draftIndex, setDraftIndex] = useState<number | null>(null);
  /** The level asked for, while the request is in flight. Never used as the
   *  chip's label — the label stays transcript truth. */
  const [pendingEffort, setPendingEffort] = useState<string | null>(null);
  /** Die Stufe, die das Backend zuletzt BESTAETIGT hat — zusammen mit dem
   *  usage-Ereignis, das in dem Moment galt.
   *
   *  Warum es das braucht (Review 20.08.2026): `capabilities` kommt aus
   *  historyQuery.data und wird nur bei `session_changed` neu geholt. Eine
   *  frische Sitzung hat noch kein usage-Ereignis, der Chip stand also auf dem
   *  persistierten Standard — und blieb dort, auch nachdem der Operator
   *  umgeschaltet hatte und das Backend den Wechsel an der CLI verifiziert
   *  hatte. Ein ruhender Agent schickt evtl. NIE ein usage; die Saeule blieb
   *  dann die ganze Sitzung falsch, und der Weg zurueck auf den alten Wert
   *  wurde von `level === currentEffort` stumm verschluckt.
   *
   *  Keine optimistische Umbenennung: gesetzt wird erst NACH dem 204, das das
   *  Backend nur gibt, wenn die CLI ihre eigene Bestaetigungszeile geschrieben
   *  hat. Und es gewinnt nur, solange kein NEUERES usage-Ereignis da ist —
   *  das kennt den laufenden Zug und damit auch die session-only-Stufen. */
  const [confirmedEffort, setConfirmedEffort] =
    useState<{ level: string; usageAt: UsageEvent | null } | null>(null);
  /** Flips to false the first time the backend answers `input_not_supported`
   *  (host agents have no pane to drive). Only the backend knows, so the chip
   *  starts interactive and demotes itself to a labelled read-only value —
   *  better than hiding a control that works for most of the fleet. */
  const [effortSupported, setEffortSupported] = useState(true);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [paletteIndex, setPaletteIndex] = useState(0);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const isWorking = state?.status === "working";
  const hasText = text.trim().length > 0;
  /** The probe ran and could not classify the pane at all (`parse_pane_state`
   *  rule 4). "Working" is not disprovable there, so an interrupt is the
   *  truthful primary action rather than a Send that claims we know better.
   *  Deliberately NOT `!state`: a missing state event means the first probe
   *  tick hasn't landed yet, which is a loading moment, not an unreadable
   *  pane — treating it as unknown would flash a Stop on every mount. */
  const statusUnknown = state?.status === "unknown";
  /** Empty input leaves nothing to send, so the button carries the interrupt. */
  /* Stop wird angeboten, sobald "arbeitet" nicht widerlegbar ist: bestaetigtes
   * `working`, ein unlesbares Pane (`unknown`) — und neu auch jeder Agent, dessen
   * Pane wir grundsaetzlich nicht lesen koennen. Ein deaktivierter Senden-Knopf
   * behauptet dort eine Ruhe, die wir nie festgestellt haben. */
  const showStop = !hasText && (isWorking || statusUnknown || !paneObservable);
  const effortLevels = resolveEffortLevels(capabilities);
  /* Welche Stufe der Chip anzeigt. Das usage-Ereignis der laufenden Session
   * gewinnt (nur es kennt die session-only-Stufen max/ultracode); solange es
   * fehlt — eine frisch gestartete Session hat noch keines — faellt der Chip auf
   * den persistierten Standard aus settings.json zurueck, den das Backend in
   * capabilities.effort mitliefert. Kennt niemand einen Wert, steht dort "auto":
   * genau das, was die CLI ohne Override tut. Vorher hing der GANZE Chip an
   * usage?.effort — ohne usage war der Effort schlicht nicht schaltbar
   * (Operator-Befund 18.08.2026). */
  /* Popovers schliessen wie ueberall sonst im System: Klick daneben oder
   * Escape. Vorher blieben Modell-Dropdown und Effort-Regler offen, bis man
   * den Chip erneut traf (kritischer Test-Durchgang 18.08.2026). Escape
   * verwirft beim Regler auch die Zieh-Vorschau, damit der anschliessende
   * blur-Commit nichts Ungewolltes sendet (draftIndex zuerst nullen). */
  useEffect(() => {
    if (!modelOpen && !effortOpen) return;
    const onPointerDown = (e: PointerEvent) => {
      const t = e.target as Node;
      if (modelBoxRef.current?.contains(t) || effortBoxRef.current?.contains(t)) return;
      setDraftIndex(null);
      setModelOpen(false);
      setEffortOpen(false);
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      setDraftIndex(null);
      setModelOpen(false);
      setEffortOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [modelOpen, effortOpen]);

  /* Reihenfolge der Wahrheit: ein usage-Ereignis, das NACH unserer letzten
   * Bestaetigung kam > unsere eigene bestaetigte Stufe > der persistierte
   * Standard aus settings.json. `usageAt` haelt fest, welches usage bei der
   * Bestaetigung galt; kommt ein anderes, ist es das juengere Wissen. */
  const confirmedIsNewest = confirmedEffort != null && confirmedEffort.usageAt === usage;
  const currentEffort =
    (confirmedIsNewest ? confirmedEffort!.level : null) ?? usage?.effort ?? capabilities?.effort ?? null;

  /* Der Agent im Composer kann wechseln, ohne dass die Komponente neu
   * montiert wird — eine Bestaetigung fuer den vorigen Agenten darf dann
   * nicht stehenbleiben. */
  useEffect(() => {
    setConfirmedEffort(null);
  }, [agentId]);

  /* Position des Reglers: waehrend des Ziehens die Vorschau, sonst der aktuelle
   * Wert. Kennt der Agent noch keine Stufe (frische Session, kein usage), steht
   * der Regler auf der ersten — angezeigt wird trotzdem, was wirklich gilt. */
  const currentEffortIndex = currentEffort ? effortLevels.indexOf(currentEffort) : -1;
  const sliderIndex = draftIndex ?? (currentEffortIndex >= 0 ? currentEffortIndex : 0);

  /* Fuellstand der Saeule im Brain-Knopf: Anteil der aktuellen Stufe an der
   * Stufenleiter des Harness. +1, damit schon "low" sichtbar gefuellt ist —
   * eine leere Saeule ist fuer "unbekannt/auto" reserviert. */
  /* Leiter fuer die reine ANZEIGE — unabhaengig vom Schaltrecht. resolveEffortLevels
   * gated bewusst auf canSwitchEffort (der Regler darf nur Schaltbares anbieten);
   * die Saeule des read-only-Chips braucht die Leiter trotzdem. */
  const displayLevels = (capabilities?.effortLevels ?? [])
    .map((l) => l.trim())
    .filter((l) => l.length > 0);
  const staticFillPct = (() => {
    const idx = currentEffort ? displayLevels.indexOf(currentEffort) : -1;
    return idx >= 0 && displayLevels.length > 0 ? ((idx + 1) / displayLevels.length) * 100 : 0;
  })();

  const effortFillPct =
    currentEffortIndex >= 0 && effortLevels.length > 0
      ? ((currentEffortIndex + 1) / effortLevels.length) * 100
      : 0;

  const commitSlider = () => {
    if (draftIndex == null) return;
    const level = effortLevels[draftIndex];
    setDraftIndex(null);
    if (level) selectEffort(level);
  };
  const slashCommands = resolveSlashCommands(capabilities?.slashCommands);
  const modelOptions = resolveModelOptions(capabilities?.modelOptions);

  // Palette is only ever "/<query>" with no space yet — once a space lands,
  // the user has moved on to arguments and the palette has no business
  // staying up. The query itself (everything after "/") drives the filter.
  const isSlashToken = text.startsWith("/") && !text.includes(" ");
  const paletteQuery = isSlashToken ? text.slice(1).toLowerCase() : "";
  const filteredCommands = isSlashToken
    ? slashCommands.filter((cmd) => cmd.command.slice(1).toLowerCase().startsWith(paletteQuery))
    : [];
  const paletteVisible = paletteOpen && filteredCommands.length > 0;
  const highlightedIndex = Math.min(paletteIndex, Math.max(filteredCommands.length - 1, 0));

  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    const previous = el.style.height;
    el.style.height = "auto";
    // A hidden element measures 0 (the mobile stack keeps the non-visible
    // screen mounted with `display: none`). Writing that back would pin the
    // input to its padding height forever, since this effect only re-runs on
    // `text`. Leaving it at the natural rows=1 height is correct until it is
    // measurable again.
    if (el.scrollHeight === 0) {
      el.style.height = previous;
      return;
    }
    const maxHeight = MAX_ROWS * LINE_HEIGHT_PX;
    el.style.height = `${Math.min(el.scrollHeight, maxHeight)}px`;
  }, [text]);

  // ── Anhänge ─────────────────────────────────────────────────────────────
  //
  // Der Weg ist bewusst simpel: Datei hoch, absoluten Pfad zurück, Pfad an
  // die Nachricht hängen — die CLI liest die Datei selbst. Kein neues
  // Protokoll, keine Bild-Kodierung im Prompt.
  //
  // Hochgeladen wird SOFORT beim Anhängen, nicht erst beim Senden: sonst
  // steht man nach dem Tippen vor einer Wartezeit, deren Grund man nicht
  // sieht, und ein Fehlschlag käme genau dann, wenn man ihn am wenigsten
  // gebrauchen kann.
  const [attachments, setAttachments] = useState<ChatAttachment[]>([]);
  const [uploading, setUploading] = useState(0);
  const [dragging, setDragging] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const addFiles = useCallback(
    async (files: File[]) => {
      if (files.length === 0) return;
      setUploading((n) => n + files.length);
      for (const file of files) {
        try {
          const stored = await api.chat.uploadAttachment(agentId, file);
          setAttachments((prev) => [...prev, stored]);
        } catch (err) {
          // Sichtbar, nicht still: "zu gross" ist die eine Ablehnung, die
          // beim Auswählen niemand sehen konnte.
          notify.error(err instanceof Error ? err.message : t("attachmentFailed"));
        } finally {
          setUploading((n) => Math.max(0, n - 1));
        }
      }
    },
    [agentId, t],
  );

  function handlePaste(e: React.ClipboardEvent<HTMLTextAreaElement>) {
    // Marks realer Weg: Cmd+Shift+4, dann Cmd+V ins Feld. Nur wenn wirklich
    // Dateien im Zwischenspeicher liegen — sonst bliebe normales Text-
    // Einfügen auf der Strecke.
    const files = Array.from(e.clipboardData?.files ?? []);
    if (files.length === 0) return;
    e.preventDefault();
    void addFiles(files);
  }

  function handleDrop(e: React.DragEvent) {
    const files = Array.from(e.dataTransfer?.files ?? []);
    setDragging(false);
    if (files.length === 0) return;
    e.preventDefault();
    void addFiles(files);
  }

  function handleDragOver(e: React.DragEvent) {
    if (!Array.from(e.dataTransfer?.types ?? []).includes("Files")) return;
    e.preventDefault();
    setDragging(true);
  }

  function removeAttachment(path: string) {
    // Nur aus dem Composer nehmen — die Datei auf der Platte bleibt liegen.
    // Ein Löschen-Aufruf hier würde eine Datei entfernen, die eine bereits
    // gesendete Nachricht noch referenzieren kann. Sie gehört dem Agenten und
    // verschwindet mit ihm (`delete_references_for(agent_id=…)`).
    setAttachments((prev) => prev.filter((a) => a.path !== path));
  }

  function send() {
    // Ein Anhang allein ist eine vollwertige Nachricht ("schau dir das an") —
    // Text ist dann nicht nötig.
    if (text.trim().length === 0 && attachments.length === 0) return;
    const lines = attachments.map((a) => `[Anhang: ${a.path}]`);
    const body = [text.trim(), ...lines].filter(Boolean).join("\n");
    onSend(body);
    setText("");
    setAttachments([]);
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (paletteVisible) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setPaletteIndex((i) => Math.min(i + 1, filteredCommands.length - 1));
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setPaletteIndex((i) => Math.max(i - 1, 0));
        return;
      }
      if (e.key === "Enter" || e.key === "Tab") {
        // Must NOT send/insert a tab while the palette is driving the
        // textarea — the highlighted command wins over both defaults.
        e.preventDefault();
        selectCommand(filteredCommands[highlightedIndex].command);
        return;
      }
      if (e.key === "Escape") {
        // Closes the palette only — the "/…" text the user typed stays put.
        e.preventDefault();
        setPaletteOpen(false);
        return;
      }
    }

    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  }

  function handleChange(e: React.ChangeEvent<HTMLTextAreaElement>) {
    const value = e.target.value;
    setText(value);
    if (value.startsWith("/") && !value.includes(" ")) {
      const query = value.slice(1).toLowerCase();
      const hasMatch = slashCommands.some((cmd) => cmd.command.slice(1).toLowerCase().startsWith(query));
      // Highlight resets to the first match on every keystroke — an empty
      // result closes the palette entirely rather than showing "no matches".
      setPaletteOpen(hasMatch);
      setPaletteIndex(0);
    } else if (paletteOpen) {
      setPaletteOpen(false);
    }
  }

  function selectModel(name: string) {
    onSend(`/model ${name}`);
    setModelOpen(false);
  }

  async function selectEffort(level: string) {
    setEffortOpen(false);
    if (level === currentEffort) return;
    setPendingEffort(level);
    try {
      await api.chat.setEffort(agentId, level);
      // Keine optimistische Umbenennung — aber eine BESTAETIGTE ist keine
      // Behauptung: das Backend antwortet erst 204, nachdem es die eigene
      // Bestaetigungszeile der CLI gelesen hat. Bis ein neueres
      // usage-Ereignis etwas anderes sagt, ist das die ehrlichste Zahl, die
      // wir haben (s. confirmedEffort).
      setConfirmedEffort({ level, usageAt: usage });
    } catch (err) {
      if (isInputNotSupportedError(err)) {
        setEffortSupported(false);
      } else if (isAgentBusyError(err)) {
        // The backend refuses mid-turn rather than interrupting the agent
        // (its own preflight). Nothing failed — the moment was wrong — so this
        // is an info, not the red persistent toast a real failure gets.
        notify.info(t("effortAgentBusy"));
      } else if (isEffortSwitchRejectedError(err)) {
        // The CLI said no AND said why. Passing its own words through beats any
        // wording of ours: it names the actual constraint (a level the current
        // model doesn't support, for instance), which a generic failure hides.
        const reason = extractErrorMessage(err);
        notify.error(reason ? t("effortRejectedReason", { reason }) : t("effortRejected"));
      } else if (isEffortSwitchFailedError(err)) {
        notify.error(t("effortUnconfirmed"));
      } else {
        notify.error(t("effortFailed"));
      }
    } finally {
      setPendingEffort(null);
    }
  }

  function selectCommand(command: string) {
    setPaletteOpen(false);
    setText(`${command} `);
    textareaRef.current?.focus();
  }

  /* Modell-Label: laufende Session > persistierter Standard aus settings.json
   * (capabilities.model) > "—". Gleiche Kette wie beim Effort — vorher zeigte
   * jede frische Session "—", obwohl das Modell laengst feststand (Operator-
   * Befund 18.08.2026). Steht dort ein Kurz-Alias ("sonnet"), zeigen wir das
   * Label des passenden Dropdown-Eintrags ("Sonnet"); eine volle ID bleibt
   * verbatim. */
  const currentModel = usage?.model ?? capabilities?.model ?? null;
  const modelLabel = currentModel
    ? modelOptions.find((m) => m.name === currentModel)?.label ?? currentModel
    : "—";
  // Backend-stamped, never a frontend model→window guess (see claudeCommands.ts).
  const win = usage?.contextWindow;
  const hasWin = typeof win === "number" && win > 0;

  // CLI ground truth wins when the backend has it; otherwise fall back to
  // our own tokens/window estimate; otherwise there's nothing honest to show.
  const hasCliPct = typeof usage?.usedPct === "number";
  const fallbackPct =
    usage && hasWin ? Math.min((usage.inputTokens / win) * 100, 100) : null;
  const pct = hasCliPct ? (usage!.usedPct as number) : fallbackPct;
  const pctSource: "cli" | "estimate" | null = hasCliPct
    ? usage?.source === "estimate"
      ? "estimate"
      : "cli"
    : fallbackPct != null
      ? "estimate"
      : null;

  const ringThresholdValue = pct != null ? ringThreshold(pct) : "normal";
  const ringStrokeColor = ringColor(ringThresholdValue);
  const ringOffset =
    pct != null ? RING_CIRCUMFERENCE * (1 - Math.min(Math.max(pct, 0), 100) / 100) : RING_CIRCUMFERENCE;

  const tokenDetail =
    usage && hasWin
      ? t("contextTokensUsed", {
          used: formatCompactTokens(usage.inputTokens),
          win: formatCompactTokens(win),
        })
      : "";
  // "CLI" ist in beiden Sprachen dasselbe Wort und bleibt darum ein Literal —
  // uebersetzt wird nur die Alternative dazu.
  const sourceLabel = pctSource === "estimate" ? t("contextSourceEstimate") : "CLI";
  const ringTitle =
    pct != null
      ? t("contextRingTooltip", { detail: tokenDetail, source: sourceLabel })
      : undefined;

  return (
    <div
      data-testid={`composer-${agentId}`}
      // The composer is the app's own floor, not a strip bolted to the
      // timeline: no top border, the pill inside carries the edge.
      //
      // (Hier stand frueher "No safe-area padding here — the app's bottom tab
      // bar already owns env(safe-area-inset-bottom)". Das gilt nicht mehr:
      // auf dem Chat-Schirm blendet AppShell die Tab-Leiste aus, es gibt also
      // nichts mehr, was den Streifen besitzt.)
      //
      // pb-safe-bottom (nur Handy): iOS reserviert unten ~34 px fuer den
      // Home-Balken. Bisher endete der Composer DARUEBER und der Streifen
      // blieb tote Flaeche. Jetzt traegt der Container den Zuschlag, die
      // Pille laeuft mit ihrer Rundung bis an die Kante heran statt darueber
      // zu schweben (Operator-Wunsch 19.08.2026, iPhone 15). Der Zuschlag
      // sitzt bewusst am Container und nicht an der Pille — sonst wuerde die
      // Pille selbst hoeher, statt naeher an den Rand zu ruecken.
      className="relative px-3 pt-2 pb-safe-bottom md:pb-4 md:px-4"
    >
      {paletteVisible && (
        <div
          data-testid="slash-palette"
          // Anchored directly above the input: bottom edge sits on the
          // composer container's top edge (= the textarea's top, since the
          // palette itself is taken out of flow), same horizontal span as
          // the textarea (left-3/right-3 mirror the container's own
          // padding), with a 320px floor for narrow layouts.
          //
          // `position: "absolute"` is set INLINE, not just via the
          // Tailwind class: `.corner-ticks` in globals.css sets
          // `position: relative` unlayered (plain CSS, not `@layer
          // utilities`), which beats Tailwind v4's own `.absolute` utility
          // under CSS cascade-layer rules regardless of source order —
          // silently downgrading this to an in-flow relative box, which is
          // what made the palette render detached mid-screen instead of
          // pinned to the input. Inline style always wins, so this can't
          // regress again. (Note for whoever owns CommandPalette.tsx: the
          // ⌘K palette combines `fixed` + `corner-ticks` the same way and
          // likely has the identical latent bug — out of scope here.)
          className="absolute bottom-full left-3 right-3 mb-2 min-w-[320px] rounded-md overflow-hidden corner-ticks z-20"
          style={{
            position: "absolute",
            backgroundColor: C.bgElevated,
            border: `1px solid ${C.border}`,
            boxShadow: "var(--shadow-elevated)",
          }}
        >
          <div className="max-h-56 overflow-y-auto p-1.5">
            {filteredCommands.map((cmd, i) => (
              <button
                key={cmd.command}
                type="button"
                data-testid={`slash-item-${cmd.command}`}
                data-highlighted={i === highlightedIndex}
                onMouseEnter={() => setPaletteIndex(i)}
                onClick={() => selectCommand(cmd.command)}
                className="w-full flex items-center gap-2 px-2 py-1.5 rounded-sm text-xs cursor-pointer font-mono text-left"
                style={{
                  backgroundColor: i === highlightedIndex ? C.accentSubtle : "transparent",
                }}
              >
                <span style={{ color: C.accent }}>{cmd.command}</span>
                <span className="text-[10px] font-medium" style={{ color: C.textMuted }}>
                  {cmd.description}
                </span>
              </button>
            ))}
          </div>
        </div>
      )}

      {/* The pill: one container holding the input and its controls, so the
          whole thing reads as a single field instead of a toolbar sitting
          under a textarea. Radius is the system cap (10px, --radius-xl).
          The white frame the operator saw was NOT this component's own ring —
          measured live: globals.css draws `outline: 2px solid #EBEBDE` with a
          2px offset on every `:focus-visible` element, and a textarea matches
          that pseudo-class even on a plain mouse click. So a near-white,
          offset, 2px halo appeared just inside the pill on every keystroke
          session. Removing only the local accent border would have left it.
          Now: that outline is suppressed here, and focus is carried by the
          pill's own frame stepping to a neutral grey (text-muted, 4.6:1
          against the pill — perceivable per WCAG 2.4.11 without being a halo,
          and unmistakably grey rather than white). */}
      <div
        data-testid="composer-dropzone"
        onDragOver={handleDragOver}
        onDragLeave={() => setDragging(false)}
        onDrop={handleDrop}
        className="flex flex-col transition-colors relative"
        // One step above the island tone: the panel is bg-surface, so the
        // control lifts to bg-elevated rather than sinking into it.
        style={{
          backgroundColor: C.bgElevated,
          borderRadius: "var(--radius-xl)",
          // Beim Ziehen tritt derselbe Rahmen in die Akzentfarbe — kein
          // zusaetzliches Overlay, das die Pille verdeckt und beim Loslassen
          // flackert. Die Pille SELBST ist das Ziel, das sagt sie damit.
          border: `1px solid ${dragging ? C.accent : focused ? C.textMuted : C.border}`,
        }}
      >
        {(attachments.length > 0 || uploading > 0) && (
          /* Kacheln ueber dem Textfeld, innerhalb der Pille: der Anhang
             gehoert sichtbar zur Nachricht, die man gerade schreibt — nicht
             in eine eigene Leiste daneben. */
          <div className="flex flex-wrap gap-1.5 px-3 pt-3">
            {attachments.map((a) => (
              <div
                key={a.path}
                data-testid="attachment-tile"
                className="group flex items-center gap-1.5 pl-1.5 pr-1 py-1 rounded-lg max-w-[200px]"
                style={{ backgroundColor: C.bgHover, border: `1px solid ${C.border}` }}
              >
                {a.isImage ? (
                  <span
                    aria-hidden
                    className="w-6 h-6 rounded-sm shrink-0 flex items-center justify-center text-[10px]"
                    style={{ backgroundColor: C.accentSubtle, color: C.accent }}
                  >
                    IMG
                  </span>
                ) : (
                  <FileText size={14} className="shrink-0" style={{ color: C.textMuted }} />
                )}
                <span className="text-[11px] truncate min-w-0" style={{ color: C.textPrimary }}>
                  {a.name}
                </span>
                <button
                  type="button"
                  onClick={() => removeAttachment(a.path)}
                  aria-label={t("removeAttachment", { name: a.name })}
                  className="shrink-0 w-5 h-5 flex items-center justify-center rounded-sm cursor-pointer"
                  style={{ color: C.textMuted }}
                >
                  <X size={12} />
                </button>
              </div>
            ))}
            {uploading > 0 && (
              <div
                data-testid="attachment-uploading"
                className="flex items-center px-2 py-1 rounded-lg text-[11px]"
                style={{ backgroundColor: C.bgHover, color: C.textMuted }}
              >
                {uploading === 1
                  ? t("attachmentUploadingOne")
                  : t("attachmentUploadingMany", { count: uploading })}
              </div>
            )}
          </div>
        )}
        <textarea
          ref={textareaRef}
          value={text}
          onChange={handleChange}
          onKeyDown={handleKeyDown}
          onPaste={handlePaste}
          onFocus={() => setFocused(true)}
          onBlur={() => setFocused(false)}
          rows={1}
          placeholder={t("messagePlaceholder")}
          // 16px on mobile is not a taste call: anything smaller makes iOS
          // Safari zoom the viewport on focus. The placeholder and caret are
          // themed explicitly — the browser's own placeholder (50% alpha of
          // the text colour) lands under 4.5:1 on this background.
          className="w-full resize-none bg-transparent outline-none focus-visible:outline-none px-3.5 pt-3.5 text-[16px] md:text-[14px] leading-[1.5] placeholder:text-[var(--color-text-muted)]"
          style={{
            color: C.textPrimary,
            caretColor: C.accent,
            // INLINE, not a utility class: globals.css's `:where(…):focus-visible`
            // rule is unlayered plain CSS, and unlayered CSS beats Tailwind's
            // `@layer utilities` no matter the specificity — the same cascade-
            // layer trap the slash palette hit with `position: absolute` (see
            // its comment above). `focus-visible:outline-none` was silently
            // ignored; only an inline value wins. Measured, not assumed.
            outline: "none",
            // Two lines of room at rest, not one: a one-line slot made a
            // multi-sentence instruction feel like the wrong place to write it.
            // Auto-grow is unaffected — it only ever sets a height ABOVE this
            // floor, and `min-height` wins whenever the content is shorter.
            minHeight: MIN_ROWS * LINE_HEIGHT_PX,
            maxHeight: MAX_ROWS * LINE_HEIGHT_PX,
          }}
        />

        <div className="flex items-center gap-1.5 px-2.5 pb-2.5 pt-1.5">
          {/* Anhaengen — ganz links, nur Symbol. Die Stelle, an der WhatsApp,
              Slack und die Claude-App ihn haben (Operator-Entscheid
              19.08.2026, Vorbild-Screenshot): man sucht ihn dort nicht, man
              greift hin.

              KEIN `accept`-Filter und KEIN `capture`: beides schneidet auf
              dem Handy Wege ab. Ohne sie zeigt iOS von selbst die Auswahl
              "Fotomediathek · Aufnehmen · Datei waehlen" — ein Knopf, alle
              drei Faelle. Und jeder Dateityp ist ohnehin erlaubt; ob der
              Agent ihn liest, ist seine Sache, nicht das Versprechen des UI. */}
          <input
            ref={fileInputRef}
            data-testid="attachment-input"
            type="file"
            multiple
            className="hidden"
            onChange={(e) => {
              void addFiles(Array.from(e.target.files ?? []));
              // Zuruecksetzen, sonst loest dieselbe Datei beim zweiten Mal
              // kein change-Ereignis aus.
              e.target.value = "";
            }}
          />
          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            aria-label={t("attachFile")}
            title={t("attachFile")}
            className="shrink-0 flex items-center justify-center w-7 h-7 rounded-lg cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)]"
            style={{ color: C.textSecondary }}
          >
            <Paperclip size={15} />
          </button>
          <div className="relative" ref={modelBoxRef}>
            {modelOptions.length === 0 ? (
              /* Fremde CLI (kimi, omp): das Backend meldet explizit keine
                 /model-Aliasse — dann ist der Chip ein ehrliches Label statt
                 eines Dropdowns, das Kauderwelsch in die TUI tippen wuerde. */
              <span
                data-testid="model-chip-static"
                className="inline-flex items-center font-mono text-xs font-medium px-2 py-1 rounded-lg"
                style={{ color: C.textMuted, border: `1px solid ${C.border}` }}
              >
                {modelLabel}
              </span>
            ) : (
            <button
              type="button"
              onClick={() => setModelOpen((v) => !v)}
              aria-haspopup="dialog"
              aria-expanded={modelOpen}
              // Quiet by default: this is a switcher, not an active state, so
              // it doesn't get the accent tint (which in this system means
              // "selected"). It lights up on hover and while open.
              className="inline-flex items-center gap-1 font-mono text-xs font-medium px-2 py-1 rounded-lg cursor-pointer transition-colors"
              style={{
                backgroundColor: modelOpen ? C.bgHover : "transparent",
                color: modelOpen ? C.textPrimary : C.textSecondary,
                border: `1px solid ${C.border}`,
              }}
            >
              {modelLabel}
              <ChevronDown
                size={11}
                className="transition-transform duration-150"
                style={{ transform: modelOpen ? "rotate(180deg)" : undefined }}
              />
            </button>
            )}
            {modelOptions.length > 0 && modelOpen && (
              <div
                role="listbox"
                className="absolute bottom-full left-0 mb-1.5 w-36 rounded-lg overflow-hidden z-20 p-1"
                style={{
                  backgroundColor: C.bgElevated,
                  border: `1px solid ${C.border}`,
                  boxShadow: "var(--shadow-elevated)",
                }}
              >
                {modelOptions.map((m) => (
                  <button
                    key={m.name}
                    type="button"
                    role="option"
                    aria-selected={m.name === currentModel}
                    data-model={m.name}
                    onClick={() => selectModel(m.name)}
                    className="w-full flex items-center gap-3 text-left px-2 py-1.5 text-[12px] font-mono rounded-md cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)]"
                    style={{
                      color: m.name === currentModel ? C.accent : C.textPrimary,
                      backgroundColor: m.name === currentModel ? C.accentSubtle : "transparent",
                    }}
                  >
                    <span className="min-w-0 truncate">{m.label}</span>
                    {/* The window belongs next to the model it describes —
                        "which one has room for this task" is the actual question
                        behind switching. Right-aligned and muted so the list
                        still reads as a list of models, with a fact attached.
                        Unknown window = no suffix; never an invented number. */}
                    {m.contextWindow != null && (
                      <span
                        className="ml-auto shrink-0 tabular-nums"
                        style={{ color: C.textMuted }}
                      >
                        {formatCompactTokens(m.contextWindow)}
                      </span>
                    )}
                  </button>
                ))}
              </div>
            )}
          </div>

          {usage && pct != null && (
            // The ring stays the compact indicator and the tooltip stays the
            // quick glance; the click opens the breakdown — "how full" and
            // "with what" are two questions, and only the first fits in 18px.
            // `relative` is what the desktop popover anchors to.
            <div className="relative shrink-0">
              <button
                type="button"
                data-context-trigger
                aria-haspopup="dialog"
                aria-expanded={contextOpen}
                // The ring's own `role="progressbar"` is unreachable inside a
                // button (the button's name wins), so the figure has to live in
                // that name — otherwise a screen-reader user learns only that
                // there is a breakdown, never how full the window is.
                aria-label={t("contextUsed", { pct: Math.round(pct) })}
                onClick={() => setContextOpen((v) => !v)}
                className="flex items-center gap-1 pl-0.5 cursor-pointer rounded-md"
              >
                <div
                  data-testid="context-ring"
                  role="progressbar"
                  aria-valuenow={Math.round(pct)}
                  aria-valuemin={0}
                  aria-valuemax={100}
                  data-threshold={ringThresholdValue}
                  data-source={pctSource ?? undefined}
                  title={ringTitle}
                  className="flex items-center gap-1 shrink-0"
                >
                  <svg width={RING_SIZE} height={RING_SIZE} viewBox={`0 0 ${RING_SIZE} ${RING_SIZE}`}>
                    <circle
                      cx={RING_SIZE / 2}
                      cy={RING_SIZE / 2}
                      r={RING_RADIUS}
                      fill="none"
                      stroke={C.bgHover}
                      strokeWidth={RING_STROKE}
                    />
                    <circle
                      data-testid="context-ring-arc"
                      cx={RING_SIZE / 2}
                      cy={RING_SIZE / 2}
                      r={RING_RADIUS}
                      fill="none"
                      stroke={ringStrokeColor}
                      strokeWidth={RING_STROKE}
                      strokeLinecap="round"
                      strokeDasharray={RING_CIRCUMFERENCE}
                      strokeDashoffset={ringOffset}
                      transform={`rotate(-90 ${RING_SIZE / 2} ${RING_SIZE / 2})`}
                    />
                  </svg>
                  <span
                    data-testid="context-ring-pct"
                    className="font-mono text-[10px] font-medium tabular-nums"
                    style={{ color: C.textMuted }}
                  >
                    {Math.round(pct)}%
                  </span>
                </div>
              </button>
              {contextOpen && (
                <ContextPanel
                  usage={usage}
                  pct={pct}
                  pctSource={pctSource}
                  onClose={() => setContextOpen(false)}
                />
              )}
            </div>
          )}

          {/* ONE morphing primary button, the Claude Code / Desktop pattern.
              Two buttons side by side asked the operator to aim; a single
              control at a fixed spot means the thing under the cursor is always
              "the thing to do next".

              Empty input while the agent works => Stop, because there is
              nothing to send and interrupting is the only useful action. Same
              for an UNCLASSIFIABLE pane (`status: "unknown"`): working can't be
              disproved there, and a disabled Send would assert idleness we
              haven't established.
              The moment there IS text, it becomes Send even mid-turn: steering
              a working agent with a normal message is legitimate and is exactly
              what the terminal allows. Idle with empty input => a ghost Send,
              since the accent means "this is the action" and there is none yet.

              Nothing at all once the session has ended — neither sending nor
              interrupting can reach a session that is over.

              Host agents (Boss, Hermes, Jarvis) are covered via
              `paneObservable={false}`: `capture_pane` returns None for them, so
              `_compute_pane_state` reports a confident `working`/`idle` from
              transcript mtime alone and never `unknown`
              (transcript_chat.py:1285-1291) — a Boss that thinks without writing
              read as `idle` and left no way to interrupt. The cost is a Stop that
              is also visible while such an agent rests; operator-decided
              (18.08.2026): a control that is missing when you need it is worse
              than one that waits. */}
          <div className="ml-auto flex items-center gap-1.5">
          {(currentEffort ||
            (effortSupported && effortLevels.length > 0) ||
            /* Der Chip erscheint AUCH ohne bekannte Stufe, sobald das Backend
               einen Grund mitliefert. Genau dieser Fall — openclaude auf einem
               Modell ohne Effort-Stufen — hatte vorher weder Wert noch Regler
               und verschwand damit spurlos; der Tooltip ist die einzige Stelle,
               an der die Erklaerung ueberhaupt ankommt.

               OHNE Kopplung an `displayLevels`: das Backend liefert fuer
               `foreign_harness` (kimi, omp) und `no_pane` (Hermes, Jarvis) hart
               eine LEERE Stufenliste — die Bedingung schloss also ausgerechnet
               zwei der drei Begruendungen aus, und sie erreichten die
               Oberflaeche nie. Ein Grund ohne Stufen ist kein Widerspruch,
               sondern der Normalfall: die Saeule bleibt dann leer (nichts
               behaupten), der Satz steht trotzdem da. */
            capabilities?.effortReason) &&
            // A picker needs levels to offer. No capabilities, `canSwitchEffort:
            // false`, or an empty list all mean the same thing here: show the
            // level, don't pretend it can be changed. Umgekehrt gilt: kann der
            // Agent umschalten, gehoert der Chip hin — auch bevor die Session
            // ihr erstes usage-Ereignis geschrieben hat.
            (effortSupported && effortLevels.length > 0 ? (
              <div className="relative" ref={effortBoxRef}>
                <button
                  type="button"
                  onClick={() => setEffortOpen((v) => !v)}
                  aria-haspopup="listbox"
                  aria-expanded={effortOpen}
                  aria-label={t("effortLevelCurrent", { level: currentEffort ?? "auto" })}
                  data-testid="effort-chip"
                  data-level={currentEffort ?? "auto"}
                  data-pending={pendingEffort != null}
                  disabled={pendingEffort != null}
                  className="inline-flex items-center justify-center gap-1 w-12 h-9 md:h-8 rounded-full cursor-pointer transition-colors disabled:cursor-wait"
                  style={{
                    backgroundColor: effortOpen ? C.bgHover : "transparent",
                    color: pendingEffort != null ? C.textMuted : effortOpen ? C.textPrimary : C.textSecondary,
                    border: `1px solid ${C.border}`,
                    opacity: pendingEffort != null ? 0.6 : 1,
                  }}
                >
                  <Brain size={15} strokeWidth={1.75} aria-hidden="true" />
                  {/* Fuellstands-Saeule (Vorbild Claude-Desktop-App): die Hoehe
                      des gruenen Segments IST die Stufe — von unten gefuellt,
                      low = kleiner Rest, ultracode = voll. Ohne bekannte Stufe
                      ("auto") bleibt die Saeule leer: nichts behaupten. Der
                      exakte Wert steht beim Oeffnen am Regler und fuer
                      Screenreader im aria-label. */}
                  <span
                    aria-hidden="true"
                    className="relative inline-block w-[4px] h-[15px] rounded-full overflow-hidden shrink-0"
                    style={{ backgroundColor: C.bgHover, border: `1px solid ${C.borderSubtle}` }}
                  >
                    <span
                      data-testid="effort-gauge-fill"
                      className="absolute bottom-0 left-0 right-0 rounded-full transition-[height] duration-200"
                      style={{
                        height: `${effortFillPct}%`,
                        backgroundColor: STATUS.online,
                      }}
                    />
                  </span>
                </button>
                {effortOpen && (
                  /* Schieberegler statt Liste (Operator-Wunsch 18.08.2026, nach
                     dem Vorbild der Claude-Desktop-App). Die Stufen sind eine
                     ECHTE Reihenfolge — von sparsam nach gruendlich — und ein
                     Regler zeigt diese Ordnung, wo eine Liste sie nur behauptet.
                     Die Stufen kommen weiterhin ausschliesslich aus den
                     Capabilities des Harness, nichts davon steht hier fest. */
                  <div
                    data-testid="effort-menu"
                    className="absolute bottom-full right-0 mb-1.5 w-64 rounded-lg z-20 p-3"
                    style={{
                      backgroundColor: C.bgElevated,
                      border: `1px solid ${C.border}`,
                      boxShadow: "var(--shadow-elevated)",
                    }}
                  >
                    <input
                      type="range"
                      min={0}
                      max={effortLevels.length - 1}
                      step={1}
                      value={sliderIndex}
                      list="effort-ticks"
                      data-testid="effort-slider"
                      aria-label={t("effortLevel")}
                      aria-valuetext={effortLevels[sliderIndex]}
                      disabled={pendingEffort != null}
                      autoFocus
                      /* Ziehen aendert nur die Vorschau. Gesendet wird erst beim
                         Loslassen — sonst schickt ein einziger Zug ueber den
                         Regler fuenf Umschalt-Befehle in die TUI. */
                      onChange={(e) => setDraftIndex(Number(e.target.value))}
                      onPointerUp={commitSlider}
                      /* Tastatur: Pfeiltasten sind VORSCHAU — sonst schickt
                         jede einzelne Pfeiltaste einen Umschalt-Befehl in die
                         TUI. Bestaetigt wird mit Enter (oder implizit beim
                         Verlassen des Reglers); Escape verwirft (siehe
                         Dokument-Listener oben, der draftIndex zuerst nullt). */
                      onKeyUp={(e) => { if (e.key === "Enter") commitSlider(); }}
                      onBlur={commitSlider}
                      className="w-full cursor-pointer accent-[var(--color-accent)] disabled:cursor-wait"
                    />
                    <div className="flex justify-between mt-1 font-mono text-[10px]" style={{ color: C.textMuted }}>
                      <span>{effortLevels[0]}</span>
                      <span>{effortLevels[effortLevels.length - 1]}</span>
                    </div>
                    <div className="mt-2 flex items-baseline gap-2">
                      <span
                        data-testid="effort-slider-value"
                        className="font-mono text-xs"
                        style={{ color: C.accent }}
                      >
                        {effortLevels[sliderIndex]}
                      </span>
                      {/* Pro Stufe, nicht pauschal: low/medium/high/xhigh
                          schreiben den persistierten Standard des Agenten um,
                          max/ultracode gelten nur fuer die laufende Session
                          (CLI-Design, empirisch belegt in agent_chat_input.py).
                          Genau diese Frage — "ueberlebt das meine Session?" —
                          muss am gewaehlten Wert stehen, nicht im Kleingedruckten. */}
                      <span className="text-[10px]" style={{ color: C.textMuted }}>
                        {isSessionOnlyEffort(effortLevels[sliderIndex])
                          ? t("effortSessionOnly")
                          : capabilities?.effortShared
                            ? t("effortBecomesDefaultShared")
                            : t("effortBecomesDefault")}
                      </span>
                      {pendingEffort != null && (
                        /* "…" solange der Wechsel unterwegs ist. Ein Haken
                           erscheint nie vorab — bestaetigt wird erst, was das
                           Transkript meldet. */
                        <span className="ml-auto text-[10px]" style={{ color: C.textMuted }}>
                          {t("effortApplying")}
                        </span>
                      )}
                    </div>
                  </div>
                )}
              </div>
            ) : displayLevels.length > 0 || capabilities?.effortReason ? (
              /* Read-only-Variante des Brain-Chips (Operator-Wunsch 18.08.2026:
                 Boss zeigte das nackte Alt-Label). Gleiche Optik wie der
                 schaltbare Knopf — Gehirn + Saeule — aber als span ohne Aktion:
                 die Leiter kommt vom Backend (canSwitchEffort=false heisst
                 "kennt der Harness", nicht "darfst du druecken"), die Stufe aus
                 dem usage-Ereignis. Der Tooltip sagt ehrlich, warum hier nichts
                 zu klicken ist.

                 Auch OHNE Stufenleiter (fremde CLI, Runtime ohne Terminal):
                 dann bleibt die Saeule leer — `staticFillPct` ist 0, sobald
                 keine Stufe zuzuordnen ist — und der Chip traegt nur noch den
                 Grund. Das ist der ganze Zweck des Grundes. */
              <span
                data-testid="effort-chip-static"
                data-level={currentEffort ?? "auto"}
                aria-label={`${t("effortLevelLocked", { level: currentEffort ?? "auto" })} — ${effortReasonText(capabilities, currentModel, t)}`}
                title={effortReasonText(capabilities, currentModel, t)}
                data-reason={capabilities?.effortReason ?? "unspecified"}
                className="inline-flex items-center justify-center gap-1 w-12 h-9 md:h-8 rounded-full cursor-default"
                style={{ color: C.textMuted, border: `1px solid ${C.border}` }}
              >
                <Brain size={15} strokeWidth={1.75} aria-hidden="true" />
                <span
                  aria-hidden="true"
                  className="relative inline-block w-[4px] h-[15px] rounded-full overflow-hidden shrink-0"
                  style={{ backgroundColor: C.bgHover, border: `1px solid ${C.borderSubtle}` }}
                >
                  <span
                    data-testid="effort-gauge-fill-static"
                    className="absolute bottom-0 left-0 right-0 rounded-full"
                    style={{ height: `${staticFillPct}%`, backgroundColor: STATUS.online }}
                  />
                </span>
              </span>
            ) : (
              /* Aeltere Backends ohne Stufenleiter in den Capabilities: ohne
                 Leiter waere jede Saeulen-Fuellung geraten — dann lieber der
                 ehrliche Text. */
              <span
                data-testid="effort-chip-static"
                title={effortReasonText(capabilities, currentModel, t)}
                data-reason={capabilities?.effortReason ?? "unspecified"}
                className="font-mono text-xs font-medium px-2 py-1 rounded-lg"
                style={{ color: C.textMuted, border: `1px solid ${C.border}` }}
              >
                {currentEffort}
              </span>
            ))}
            {sessionLive && (
              !showStop ? (
                <button
                  type="button"
                  onClick={send}
                  aria-label={t("send")}
                  disabled={!hasText}
                  data-empty={!hasText}
                  data-testid="send-button"
                  className="inline-flex items-center justify-center w-9 h-9 md:w-8 md:h-8 rounded-full cursor-pointer disabled:cursor-not-allowed transition-colors"
                  style={
                    hasText
                      ? { backgroundColor: C.accent, color: C.onAccent }
                      : {
                          backgroundColor: "transparent",
                          color: C.textDim,
                          border: `1px solid ${C.border}`,
                        }
                  }
                >
                  <ArrowUp size={16} strokeWidth={2.25} />
                </button>
              ) : (
                <button
                  type="button"
                  onClick={onStop}
                  aria-label={t("stop")}
                  title={t("interruptEsc")}
                  data-testid="stop-button-prominent"
                  data-reason={isWorking ? "working" : !paneObservable ? "unobservable" : "unknown"}
                  /* The pulse is a claim of live activity, so it only runs when
                     the probe actually confirmed "working". On an unreadable
                     pane the control is offered without animating something we
                     don't know. */
                  className={`${isWorking ? "animate-pulse " : ""}inline-flex items-center justify-center w-9 h-9 md:w-8 md:h-8 rounded-full cursor-pointer`}
                  style={{
                    backgroundColor: C.accentSubtle,
                    color: C.textPrimary,
                    border: `1px solid ${C.borderAccent}`,
                  }}
                >
                  <Square size={12} fill={C.textPrimary} />
                </button>
              )
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
