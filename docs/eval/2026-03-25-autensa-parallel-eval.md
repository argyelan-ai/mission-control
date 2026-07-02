# Autensa vs Mission Control — Parallel Evaluation

**Datum:** 2026-03-25
**Evaluator:** Claude Code (Session mit dem Operator)
**Autensa Version:** 2.4.0 (github.com/crshdn/mission-control)
**MC Version:** aktueller main Stand (Phase 4A, 438 Tests)

---

## 1. Executive Summary

| Aspekt | Mission Control (unser) | Autensa |
|--------|------------------------|---------|
| **Zweck** | Agent Command Center (Task-Orchestrierung) | Product Development Automation Platform |
| **Stack** | FastAPI + Next.js 15 + PostgreSQL + Redis + Docker | Next.js 14 (Fullstack) + SQLite |
| **Codebase** | ~53k LOC (Backend 29k + Frontend 24k) | ~40k LOC (alles TypeScript) |
| **Tests** | 438 Tests (11.7k LOC) | ~0 Tests |
| **Infrastruktur** | 5 Docker Container + Caddy | Single Node.js Process |
| **Gateway-Anbindung** | WebSocket RPC (produktiv, gehaertet) | WebSocket RPC (funktioniert, weniger gehaertet) |
| **Agents** | 10 (produktiv, mit Scopes/Permissions) | 4 Bootstrap + Gateway-Import |
| **Tasks** | 264 (Produktionsdaten) | 0 (frisch) |

**Kernaussage:** Autensa und MC sind **keine Konkurrenten** — sie loesen unterschiedliche Probleme. Autensa ist eine **Produktentwicklungs-Automatisierung** (Ideen generieren, bewerten, bauen lassen). MC ist ein **operatives Agent Command Center** (Tasks orchestrieren, Guards erzwingen, Credentials managen). Die Systeme koennten sich ergaenzen, aber ein Ersetzen in beide Richtungen wuerde massive Feature-Verluste bedeuten.

---

## 2. Feature-Matrix

### Was NUR MC hat (Autensa fehlt es)

| Feature | Bedeutung | Aufwand zu portieren |
|---------|-----------|---------------------|
| **PostgreSQL + Redis** | Skalierbar, transaktional, produktionsreif | Hoch (SQLite = Single-Writer) |
| **438 Tests** | Regressionssicherheit, Guard-Validierung | Sehr hoch (11.7k LOC) |
| **dispatch_phase Gating** | Plan-vor-Ausfuehrung erzwungen | Mittel |
| **Atomic Promote** | 6 WHERE-Conditions, Race-Condition-frei | Mittel |
| **Scope-based Permissions** | 13 Scopes, per-Agent erzwungen | Hoch |
| **Credential Encryption** | Fernet-verschluesselt, Leak-Prevention | Hoch |
| **Dispatch ACK Handshake** | 10-min Timeout, Circuit Breaker | Mittel |
| **Watchdog (7 Checks)** | Stale, Undispatched, Root-Close, Phase-Complete | Hoch |
| **Blocker Re-Dispatch** | blocked → inbox → fresh dispatch | Mittel |
| **Evidence Validation** | Substanzielle Artefakt-Pruefung | Mittel |
| **Delegation Contracts** | Pflichtfelder pro Task-Typ (422 bei Fehler) | Mittel |
| **Telegram + Discord** | Multi-Channel Operator-Interface | Mittel |
| **Git Workflow** | Auto-PRs, Branch-per-Task, Merge-on-Done | Hoch |
| **Intelligence System** | Periodische Analyse, Agent-Metrics | Mittel |
| **SOUL/TOOLS Templates** | Per-Agent Jinja2-basierte Persoenlichkeit | Niedrig |
| **CORS + Auth (JWT + PBKDF2)** | Production-grade Security | Hoch |
| **Alembic Migrations** | 45+ versionierte DB-Aenderungen | N/A (andere DB) |
| **Docker + Caddy** | Reverse Proxy, Container-Isolation | Mittel |

### Was NUR Autensa hat (MC fehlt es)

| Feature | Bedeutung | Aufwand zu portieren |
|---------|-----------|---------------------|
| **Autopilot (Research + Ideation)** | LLM generiert Ideen basierend auf Produkt-Programm | Hoch |
| **Swipe-basierte Ideenbewertung** | Tinder-artig: approve/reject/maybe | Mittel |
| **Preference Learning** | Bayesian-Modell lernt aus Swipe-History | Hoch |
| **A/B Testing fuer Strategien** | Parallel-Varianten von Produkt-Programmen | Hoch |
| **Produkt-Konzept** | Produkte als Erste-Klasse-Entitaet mit Health Score | Mittel |
| **Skill Learning Loop** | Agents lernen wiederverwendbare Prozeduren | Hoch |
| **Workspace Isolation** | Git Worktree / Sandbox pro Task | Mittel |
| **Convoy (Multi-Agent-Decomposition)** | AI-gestuetzte Task-Zerlegung mit Dependencies | Mittel |
| **Agent Mailbox** | Inter-Agent-Kommunikation innerhalb Convoys | Niedrig |
| **Checkpoint System** | Crash-Recovery mit State-Snapshots | Mittel |
| **Cost Tracking + Caps** | LLM-Kosten pro Task/Agent/Zyklus + Budgets | Mittel |
| **Health Scores** | Composite Score (Research, Pipeline, Velocity, Cost) | Mittel |
| **Workflow Templates** | Multi-Stage mit Fail-Loopback | Mittel (MC hat Ansaetze) |
| **Similarity Detection** | Feature-Hashing + Cosine fuer Ideen-Dedup | Mittel |
| **Single Process** | Kein Docker noetig, sofort lauffaehig | N/A (anderer Ansatz) |

### Was BEIDE haben (unterschiedliche Implementierung)

| Feature | MC | Autensa |
|---------|-----|---------|
| **OpenClaw Gateway** | WebSocket RPC, gehaertet | WebSocket RPC, funktional |
| **Task Management** | Status-Machine (7 States) | Status-Machine (~10 States) |
| **Agent Discovery** | gateway_sync.py (Startup) | /api/agents/discover (On-Demand) |
| **Agent Import** | Automatisch bei Sync | Manuell via /api/agents/import |
| **Task Dispatch** | chat_send / chat_send_isolated | chat.send via RPC |
| **Knowledge Base** | board_memory Tabelle, 7 Types | knowledge_entries mit Confidence |
| **Event System** | SSE + Activity Events | SSE Broadcasting |
| **Planning** | Henry-basiert (Prompt-gesteuert) | Stateless Multi-Turn via Gateway |
| **Sub-Tasks** | Parent/Child mit Phase-System | Convoy mit Dependency-Graph |
| **Deliverables** | TaskComment mit Artefakt-Pfad | Eigene Deliverables-Tabelle |
| **Dark Theme** | Ja (Design System) | Ja |
| **Real-time Updates** | SSE + TanStack Query Polling | SSE |

---

## 3. Gateway-Anbindung (Teil C Ergebnis)

| Aspekt | Ergebnis |
|--------|---------|
| **Verbindung** | Funktioniert nach Device-Pairing + Token |
| **Agent Discovery** | 5/5 Agents erkannt (main, cody, rex, spark, planner) |
| **Agent Import** | Automatisch mit source=gateway + gateway_agent_id |
| **Session Erstellung** | Neue Sessions auf Gateway erstellt (mission-control-*) |
| **Task Dispatch** | Erfolgreich — Nachricht via RPC an Agent gesendet |
| **Session-Isolation** | Eigene Session-Keys, kein Konflikt mit MC Sessions |
| **Koexistenz** | MC und Autensa koennen GLEICHZEITIG am selben Gateway arbeiten |

**Wichtig:** Autensas Device-Pairing musste manuell approved werden (`openclaw devices approve`). Der Gateway-Token muss in `.env.local` stehen.

---

## 4. Funktionstest (Teil D Ergebnis)

| Test | Ergebnis |
|------|---------|
| Autensa starten | OK (Port 4000, SQLite) |
| Gateway verbinden | OK (nach Pairing + Token) |
| Agents discovern | OK (5 Gateway-Agents) |
| Task erstellen | OK (mit Autensa-eigenen Priority-Enums) |
| Task dispatchen | OK (via Gateway RPC an Agent) |
| Modelle auflisten | OK (2 Modelle: gpt-5.3-codex, gpt-5.4) |
| UI erreichbar | OK (http://localhost:4000) |
| Keine MC-Stoerung | OK (getrennte Sessions, kein Seiteneffekt auf MC) |

---

## 5. Architekturvergleich

### MC Architektur
```
Operator → Telegram/Web → MC Backend (FastAPI)
                              ↓
                    PostgreSQL + Redis
                              ↓
                    Watchdog (7 periodische Checks)
                              ↓
                    Dispatch (Guards, ACK, Promote)
                              ↓
                    OpenClaw Gateway → Agents
                              ↓
                    Review → Evidence → Done
```
**Staerke:** Robuste Guards, transaktionale DB, systematische Recovery
**Schwaeche:** Komplex, 5 Container, kein Autopilot/Ideation

### Autensa Architektur
```
Operator → Web UI → Next.js API Routes
                        ↓
                    SQLite (single-writer)
                        ↓
                    Autopilot (Research → Ideation → Swipe)
                        ↓
                    Planning (Multi-Turn) → Convoy (Multi-Agent)
                        ↓
                    OpenClaw Gateway → Agents
                        ↓
                    Workflow (Build → Test → Review)
                        ↓
                    Skill Learning → Knowledge
```
**Staerke:** Produkt-Pipeline (Idee → Ship), Lightweight, Learning Loop
**Schwaeche:** Keine Tests, SQLite Single-Writer, kein Credential-Management, keine Channel-Integration

---

## 6. Bewertung: Was fuer den Operator relevant ist

### MC behalten — KLAR JA
MC ist das operative Rueckgrat. 438 Tests, Credential-Handling, Telegram-Integration, Guard-System. Das kann Autensa nicht ersetzen.

### Autensa als Ergaenzung — MOEGLICH
Autensas Staerken (Autopilot, Ideation, Skill Learning, Cost Tracking) sind genau die Features die MC NICHT hat und die fuer die "Jarvis-Vision" interessant waeren.

### Konkrete Uebernahme-Kandidaten

| Feature | Aufwand | Nutzen | Empfehlung |
|---------|---------|--------|------------|
| **Cost Tracking** | Mittel | Hoch (LLM-Kosten sichtbar) | Portieren nach MC |
| **Workspace Isolation** | Mittel | Hoch (parallele Builds) | Portieren nach MC |
| **Checkpoint System** | Mittel | Hoch (Crash Recovery) | Portieren nach MC |
| **Convoy Decomposition** | Mittel | Mittel (Dependencies) | Spaeter evaluieren |
| **Autopilot/Ideation** | Hoch | Mittel (cooles Feature, aber anderer Fokus) | Autensa parallel laufen lassen |
| **Skill Learning** | Hoch | Mittel-Hoch (Agents werden besser) | Langfristig portieren |
| **Health Scores** | Mittel | Niedrig-Mittel (MC hat Intelligence) | Optional |

### NICHT uebernehmen

| Feature | Grund |
|---------|-------|
| SQLite | PostgreSQL ist besser fuer MC |
| Keine Tests | MC hat 438 — Autensas 0-Test-Kultur nicht importieren |
| Next.js API Routes | FastAPI ist besser getrennt |
| Workflow Templates | MC hat eigenes Phase-System, nicht mischen |

---

## 7. Rollback-Plan (Teil F)

### Autensa komplett entfernen (< 2 Minuten)

```bash
# 1. Autensa stoppen
lsof -ti :4000 | xargs kill -9

# 2. Device vom Gateway entfernen
env -u CLAUDECODE openclaw devices remove 29ac2b2dec504fd17f3652445316f684a02979829c91a15e959e1150cd4f557c

# 3. Autensa-Dateien loeschen
rm -rf $HOME/Workspace/Projects/autensa/
rm -rf $HOME/Workspace/Projects/autensa-workspace/
rm -rf ~/.mission-control/

# 4. Keine Aenderung an MC noetig — MC wurde nicht veraendert
```

### Autensa parallel weiterlaufen lassen

```bash
# Starten
cd $HOME/Workspace/Projects/autensa && npm run dev -- --port 4000

# Stoppen
lsof -ti :4000 | xargs kill -9
```

- Kein Konflikt mit MC (getrennte Ports, getrennte DB, getrennte Sessions)
- Beide nutzen denselben Gateway — verschiedene Session-Keys
- Autensas Sessions starten alle mit `mission-control-*` Prefix

### Was bei MC NICHT veraendert wurde
- Kein Code geaendert
- Keine DB-Migration
- Keine Config-Aenderung
- Kein neuer Container
- Gateway: nur ein neues Device gepairt (kann jederzeit entfernt werden)

---

## 8. Fazit und Empfehlung

**Autensa ist kein MC-Ersatz.** Es ist ein anderes Produkt mit anderem Fokus.

**Empfehlung:**
1. **MC als primaeres System behalten** — es ist produktionsreif und gehaertet
2. **Autensa als Inspirationsquelle nutzen** — 3 Features aktiv portieren: Cost Tracking, Workspace Isolation, Checkpoint System
3. **Autensa parallel laufen lassen fuer Experimente** — kein Risiko, kein Konflikt
4. **Langfristig**: Skill Learning und Convoy-System als Erweiterung fuer MC evaluieren

**Kein Migration-Pfad empfohlen.** Die Codebasen sind zu unterschiedlich fuer einen Merge. Cherry-Picking der besten Ideen ist der richtige Ansatz.
