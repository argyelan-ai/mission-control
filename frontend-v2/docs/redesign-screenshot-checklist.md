# Redesign — Screenshot-Checkliste

> Inventar aller Seiten, Panels und Pop-ups von frontend-v2.
> Ziel: jede Ansicht wird **vorher/nachher** fotografiert — Overlays auch im
> **geöffneten Zustand**. Trigger-Spalte = wie das Panel im Screenshot-Skript
> geöffnet wird. Stand: 2026-07-17, Branch `feat/frontend-v3-redesign`.

Legende Status: ☐ offen · ☑ fotografiert

## 1. Seiten (Routen)

| Status | Route | Ansicht | Bemerkung |
|---|---|---|---|
| ☐ | `/login` | Login | ohne Auth |
| ☐ | `/` | Homepage / Übersicht | SystemHealthSection, PipelineView, ActivityFeed |
| ☐ | `/tasks` | Kanban-Board | Lanes inbox/in_progress/review/done |
| ☐ | `/inbox` | Inbox | |
| ☐ | `/agents` | Agent-Grid | AgentCards |
| ☐ | `/agents/[id]` | Agent-Detail | Tabs inkl. CliTerminalTab (xterm) |
| ☐ | `/agents/wizard` | Agent-Wizard | mehrstufig (steps/) |
| ☐ | `/office` | Office | Live-Ansicht Agenten |
| ☐ | `/sessions` | Sessions | StructuredSessionView |
| ☐ | `/insights` | Insights | Charts (recharts) |
| ☐ | `/schedule` | Schedule/Cron-Liste | |
| ☐ | `/schedule/[jobId]` | Job-Detail | |
| ☐ | `/memory` | Memory/Notizen | |
| ☐ | `/memory/graph` | Memory-Graph | react-force-graph-2d |
| ☐ | `/repos` | Repos | |
| ☐ | `/files` | Dateien | |
| ☐ | `/loops` | Loops | |
| ☐ | `/skills` | Skills | SkillMatrix |
| ☐ | `/content` | Content | |
| ☐ | `/news` | News | |
| ☐ | `/bench` | Bench | |
| ☐ | `/runtimes` | Runtimes | |
| ☐ | `/settings` | Settings | SettingsNav, CliToolsSection |

## 2. Globale Overlays (Shell)

| Status | Panel | Trigger | Bemerkung |
|---|---|---|---|
| ☐ | CommandPalette | `⌘K` / `Ctrl+K` | cmdk, global in AppShell |
| ☐ | TaskDetailPanel (SlideOver) | Klick auf Task-Karte | SlideOverPanel rechts |
| ☐ | CreateTaskModal | „Neuer Task"-Button | ResponsiveModal |
| ☐ | RuntimeSwitchModal | Runtime-Wechsel (RuntimePill) | |
| ☐ | BindAgentModal | Agent an Task binden | |
| ☐ | WorkspaceSwitcher-Dropdown | Klick auf Switcher in Sidebar | Radix Dropdown |
| ☐ | MobileNav-Drawer | Hamburger (Viewport <768px) | Mobile-only |
| ☐ | VoiceWidget | Floating Voice-Button | Overlay + Cards |
| ☐ | VoicePreviewSheet | aus VoiceWidget | Sheet |
| ☐ | Toasts (sonner) | beliebige Aktion | ToastRenderer |

## 3. Task-Detail-Tabs (im SlideOver)

| Status | Tab/Panel | Bemerkung |
|---|---|---|
| ☐ | Kommentare (TaskComments/CommentCard) | |
| ☐ | Verlauf (TaskTimeline/TaskHistory) | |
| ☐ | Deliverables (DeliverablesTab/DeliverableCard) | |
| ☐ | Workspace (WorkspaceTab, DirectoryBrowser, FilePreview) | |
| ☐ | E2E (E2ETab) | |
| ☐ | Git (GitPanel, GitInfoBox) | |
| ☐ | Transkript (TaskTranscript) | |
| ☐ | Referenzen (TaskReferences) + ProjectReferencesDialog | |
| ☐ | ReflectionForm | |

## 4. Bereichs-Panels & Dialoge

| Status | Panel/Dialog | Bereich | Trigger |
|---|---|---|---|
| ☐ | ActivityHistoryPanel | Homepage | Klick auf Activity-Eintrag |
| ☐ | JobModal | Schedule | „Neuer Job" / Job bearbeiten |
| ☐ | CreateLoopDialog | Loops | „Neuer Loop" |
| ☐ | LoopDetailPanel | Loops | Klick auf Loop |
| ☐ | ImportRepoDialog | Repos | „Repo importieren" |
| ☐ | RepoDetailPanel | Repos | Klick auf Repo |
| ☐ | FilePreviewPanel | Files | Klick auf Datei |
| ☐ | DeleteFilesDialog | Files | Löschen-Aktion |
| ☐ | PurgeTrashDialog | Files/Trash | „Papierkorb leeren" |
| ☐ | FilesActionBar | Files | erscheint bei Auswahl |
| ☐ | NoteSidePanel | Memory | Klick auf Notiz |
| ☐ | AttachmentPanel | Memory | Anhang öffnen |
| ☐ | AttachmentLightbox | Memory | Klick auf Bild-Anhang |
| ☐ | VaultReadingPanel | Vault/Memory | Klick auf Eintrag |
| ☐ | ConfirmDeleteModal | Vault | Löschen-Aktion |
| ☐ | MCPAddServerModal | Settings/MCP | „Server hinzufügen" |
| ☐ | BrowserLiveView | Office/Tasks | Live-Browser-Stream |
| ☐ | Agent-Kontextmenü | Agents | Dropdown auf AgentCard |
| ☐ | SparkRecipeSwitcher | div. | Switcher-Dropdown |

## 5. Zustände, die mitfotografiert werden

- ☐ Sidebar: normal / hover / aktiver Eintrag
- ☐ StatusBar: mit laufenden Agenten vs. leer
- ☐ StatusDot-Varianten (online/busy/error/offline)
- ☐ Leere Zustände (empty states) mind. 1× (z.B. Inbox leer)
- ☐ Lade-Zustände (Skeletons) mind. 1×
- ☐ Mobile-Ansicht (390×844): Home, Tasks, MobileNav offen, TaskDetailPanel
- ☐ Fokus-Ring sichtbar (Keyboard-Navigation, `:focus-visible`)

## 6. Technische Notizen fürs Skript

- Base-URL lokal: `http://127.0.0.1:3000` (Docker-Frontend) — **nicht**
  `localhost` (IPv6-Konflikt mit fremdem next-server auf `:3000`).
- Auth: `localStorage.setItem("mc_auth_token", …)` vor Navigation
  (Pattern aus `scripts/mobile-screenshot.ts`, `MC_TOKEN`-Env).
- `waitUntil: "domcontentloaded"` + feste Wartezeit — `networkidle` wird
  durch Websockets/Polling nie erreicht.
- Viewports: Desktop 1600×1000, Mobile 390×844.
