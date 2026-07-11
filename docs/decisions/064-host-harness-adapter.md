# ADR-064 — HostHarnessAdapter: generischer Adapter-Layer für Host-Agent-Runtime-Bindung

**Status:** Accepted
**Datum:** 2026-07-07
**Scope:** Backend/Provisioning, Backend/Runtime, Infra/Host-Bridge, Frontend/Runtimes

## Kontext

Hermes (`harness=hermes`, host-side tmux Worker, siehe ADR-029) war an eine lokale Spark-vLLM-Runtime
gebunden — aber nur **kosmetisch**: `agent.env` bekam korrekt `OPENAI_BASE_URL`/`OPENAI_MODEL`
geschrieben, aber das Hermes-Binary liest diese Env-Vars nicht. Es nimmt ausschliesslich
`~/.hermes/config.yaml` (`model.provider`/`model.base_url`/`model.default`). Ergebnis: Hermes lief
faktisch dauerhaft auf `ollama-cloud/kimi-k2.6`, unabhängig davon, was die gebundene Runtime tatsächlich
servierte.

Drei konkrete Lücken zur „automatisch folgt dem servierten Modell"-Vision (analog `omp`s
`mc-openai`-Provider + Runtime Watcher, ADR-054):

1. **Config-Render:** Nichts schrieb Provider/Modell/Endpoint in Hermes' native Config.
2. **Auto-Forward:** `runtime_propagation.mark_agents_for_sync()` übersprang Host-Agents hart
   (`if agent.agent_runtime != "cli-bridge": continue`) — Modell-Drift-Events der Runtime Watcher
   (ADR-054) erreichten Host-Agents nie als Reload, nur als informationsloses Activity-Event.
3. **Umbinden:** Host-Agents warfen bei jedem Switch-Versuch `AgentNotSwitchableError` (422) —
   `single_instance` (ADR-029) blockte pauschal jede Bindungsänderung, nicht nur die eigentlich
   gefährliche Parallel-Instanz.

Die Hermes-Bootstrap-Logik (`build_hermes_agent_env`, `bootstrap_hermes_agent`) war ausserdem hart
Hermes-spezifisch in `agent_bootstrap.py` verdrahtet und der Provisioning-Dispatch in `routers/agents.py`
verzweigte per `if runtime.runtime_type == "hermes": ... else: raise` — kein Muster, das sich auf einen
zweiten Host-Harness (z. B. Claude Code mit custom API-Endpoint) erweitern liesse, ohne den
Hermes-Pfad anzufassen.

## Entscheidung

Ein generischer **`HostHarnessAdapter`**-Layer kapselt die einzige Variabilität zwischen
Host-CLIs: ihre native Config-Oberfläche und ihr Reload-Mechanismus. Alles andere (launchctl,
`agent.env`-Write, Workspace-Layout) bleibt geteilt.

### 1. Adapter-Interface + Registry (`backend/app/services/host_harness_adapter.py`, neu)

```python
class HostHarnessAdapter(Protocol):
    harness: str          # "hermes" | "claude" | ...
    protocol: str         # "openai" | "anthropic"

    async def build_agent_env(self, agent, runtime, token, *, session) -> dict[str, str]: ...
    async def bootstrap(self, session, agent, runtime) -> dict[str, Any]: ...
    async def reload(self, agent) -> dict[str, Any]: ...

HOST_ADAPTERS: dict[str, HostHarnessAdapter] = {"hermes": HermesAdapter()}

def get_adapter(harness: str | None) -> HostHarnessAdapter | None:
    ...
```

`HermesAdapter` delegiert `build_agent_env`/`bootstrap` unverändert an die bestehenden
`build_hermes_agent_env`/`bootstrap_hermes_agent`-Funktionen (kein Verhaltenswechsel, nur Umzug hinter
das Interface) und `reload` an den bereits vorhandenen `_host_agent_lifecycle(agent, "restart")`-Pfad
(`routers/cli_terminal.py`, SSH → `hermes-bridge` `/restart`).

`sync_host_agent_model(agent, runtime, *, session)` ist der Auto-Forward-Baustein: liest die
bestehende `agent.env`, überschreibt **ausschliesslich die `OPENAI_*`-Keys** aus
`build_runtime_env(runtime, session)` und schreibt die Datei zurück. `MC_AGENT_TOKEN` und alle
anderen bestehenden Keys bleiben unangetastet — ein Modell-Sync darf den Auth-Token nie
regenerieren.

### 2. Native Config-Render — Hermes `custom`-Provider (host-seitig)

`scripts/hermes-config-patch.py` bekommt einen `_model_patches_from_env()`-Zweig: liest
`OPENAI_BASE_URL`/`OPENAI_MODEL` aus der (bereits gesourcten) `agent.env` und patcht
`model.provider=custom` + `model.base_url` + `model.default` in `~/.hermes/config.yaml`. Hermes hat
einen eingebauten `custom`-Provider, der `model.base_url` direkt liest — kein `providers`-Eintrag
nötig.

**Guard:** fehlt eine der beiden Env-Vars, bleibt der `model:`-Block unangetastet — eine bewusste
Handkonfig (z. B. Cloud) wird nicht überschrieben. Idempotent: gleiche Env → gleiche `config.yaml`.

`docker/hermes/entrypoint.sh` ruft den Patcher **nach** `source agent.env` und **vor** dem
Hermes-Start auf — bei **jedem** (Re-)Start der `hermes-worker`-tmux-Session. Damit wird der
Reload-Pfad trivial: `agent.env` neu schreiben + Session neu starten = neues Modell live, kein
separater Config-Sync-Code nötig.

### 3. Provisioning-Dispatch (`routers/agents.py`)

Der harte `if runtime.runtime_type == "hermes"`-Branch wird durch einen Registry-Lookup ersetzt:

```python
adapter = get_adapter(harness)
if adapter is None:
    raise HTTPException(400, ...)          # unbekannter Harness
if not is_compatible(harness, runtime):
    raise HTTPException(422, ...)          # Protokoll-Mismatch (z.B. anthropic-Runtime für hermes)
```

`is_compatible()`/`incompat_reason()` (aus `harness_compat.py`, ADR-056) laufen jetzt auch am
Host-Provisioning-Einstieg, nicht nur beim cli-bridge-Switch — ein openai↔anthropic-Mismatch schlägt
mit klarer 422-Meldung fehl statt still falsch zu starten.

### 4. Auto-Forward für Host-Agents (`runtime_propagation.py`)

`mark_agents_for_sync()` flaggt jetzt auch Host-Agents mit registriertem Adapter
(`agent.agent_runtime == "host" and get_adapter(agent.harness or derive_harness(runtime))`), nicht mehr
nur cli-bridge. Der Sync-Pass verzweigt: idle Host-Agents bekommen `adapter.build_agent_env()` (via
`sync_host_agent_model`) + `adapter.reload()` statt `sync_docker_agent_files()` + `docker restart`.
Busy Agents (`current_task_id` gesetzt) bleiben `pending_runtime_sync=true` und werden vom nächsten
Watcher-Tick erneut versucht — kein Task-Abbruch mitten drin, gleiches Pattern wie beim cli-bridge-Pfad
(ADR-054). Ein manueller **„Host-Agent neu laden"-Button** im Frontend triggert denselben Host-Sync-Pfad
sofort.

### 5. `single_instance`-Präzisierung — Amendment zu ADR-029

ADR-029 führte `runtimes.single_instance` ein mit der Intention „keine zweite Instanz gegen dasselbe
`~/.hermes/state.db`" — implementiert wurde daraus aber ein **pauschaler** Switch-Block: jede
Bindungsänderung an oder von einer `single_instance`-Runtime warf `AgentNotSwitchableError (422)`,
auch das reine Umbinden **desselben** Agents.

**Präzisierung:** `single_instance` bedeutet „keine **parallele** Instanz", nicht „kein Umbinden".
Ein neuer Helper `_is_host_inplace(agent)` (`agent_runtime_switch.py`) erkennt Host-Agents mit
registriertem Adapter und leitet sie auf einen **In-Place-Switch** um:

```
switch(agent=hermes, new_runtime)
  ├─ Guard: is_compatible(agent.harness, new_runtime)?         # sonst 422
  ├─ Guard: adapter vorhanden?                                  # sonst 422
  ├─ Redis-Lock mc:agent:{id}:runtime-switch (TTL 120s)
  ├─ In-Progress-Check: agent busy? → 409 (ohne Force)
  ├─ agent.runtime_id = new_runtime.id → commit
  ├─ adapter.build_agent_env() via sync_host_agent_model() → agent.env neu
  ├─ adapter.reload()  # kill → re-source agent.env → re-patch config.yaml → restart
  └─ Fail → voller Rollback (runtime_id + agent.env) + agent.runtime_switch_failed Event
```

Der Ablauf ist **strikt sequenziell** — alte Session killen, dann neu starten — es existiert zu keinem
Zeitpunkt ein zweiter Hermes-Prozess gegen dieselbe State-DB. Damit bleibt die ADR-029-Intention
gewahrt, ohne den generischen Fall (jemand versucht einen **zweiten** Agent an dieselbe
`single_instance`-Runtime zu binden — Parallelität) zu lockern: dieser Fall bleibt ein hartes 422.
`agent_runtime_switch.py` prüft `single_instance` daher nur noch **wenn `not is_host_inplace`**.

**ollama-cloud als reguläre Runtime:** registriert als `runtime_type: cloud` (openai-kompatibel,
`endpoint: https://ollama.com/v1`, `model_identifier: kimi-k2.6`), also ein normales
Protokoll-kompatibles Switch-Ziel für Hermes — Umschalten Spark ↔ ollama-cloud läuft über denselben
In-Place-Pfad.

### 6. Kein LiteLLM / Protokoll-Shim

Der Adapter übersetzt nichts zwischen Protokollen. `protocol: "openai" | "anthropic"` wird über
`harness_compat.is_compatible()` (ADR-056) gegen die Runtime geprüft — ein Harness bekommt nur
Runtimes seines eigenen Protokolls angeboten. Ein anthropic-`/v1/messages`-Shim für lokale
OpenAI-Modelle bliebe (wie in ADR-056 §v2 vermerkt) explizit ausserhalb dieser Runde: lokale
OpenAI-Modelle laufen über die OpenAI-nativen Harnesses (Hermes/omp), nicht über einen übersetzten
Claude-Code-Pfad.

### 7. `ClaudeCodeHostAdapter` — designed, nicht implementiert

`get_adapter("claude")` liefert bewusst `None`. Die Erweiterungsstelle ist dokumentiert (nicht
gebaut): `build_agent_env` würde `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_MODEL` aus der
Runtime-Bindung rendern (bei Cloud-OAuth-Runtimes kein Inject — OAuth-Keychain bleibt), `reload` würde
`launchctl kickstart -k gui/<uid>/<label>` nutzen, `protocol = "anthropic"`. **Boss bleibt in dieser
Runde vollständig unangetastet** — `docker/boss-host/start-claude.sh` (hartcodiertes
`ANTHROPIC_MODEL`, reine OAuth) ist nicht Teil dieses Changes. Der Adapter existiert erst, wenn
jemand opt-in einen Host-`claude`-Agent an eine custom Runtime binden will.

## Alternativen

- **Pro-Host-CLI Hardcode-Branches** (`if runtime.runtime_type == "hermes": ...` überall erweitern).
  Verworfen — genau das Muster, das schon vor diesem Change bestand und das jede zukünftige
  Host-Integration (Claude Code mit custom Endpoint) zu einer Hermes-Bridge-Änderung mit
  entsprechendem Blast-Radius gemacht hätte.
- **LiteLLM/Protokoll-Shim, damit jeder Harness jedes Modell sprechen kann.** Verworfen für v1 —
  Harness wird zum Protokoll gewählt, nicht übersetzt; ein Shim ist zusätzliche Infrastruktur
  (eigener Prozess, Fehlerquelle, Latenz) für einen Fall, den es heute nicht gibt (kein Nutzer
  verlangt aktuell Claude Code gegen ein lokales OpenAI-Modell).
  Bewusst als eigenständige, erstklassige Runtime für später vermerkt, nicht drangeflanscht.
- **`single_instance` generell aufweichen/entfernen.** Verworfen — der ursprüngliche
  Parallel-Instanz-Schutz aus ADR-029 ist weiterhin nötig (zwei Hermes-Prozesse gegen dieselbe
  `state.db` korrumpieren State); die Präzisierung engt den Boolean nur auf seinen eigentlichen Zweck
  ein, statt ihn zu entfernen.
- **Host-Pull statt Backend-Push für den Reload.** Bridge pollt periodisch ein Sync-Flag und
  restartet sich selbst. Verworfen (offen im Design-Doc vermerkt, aber entschieden) — Push via
  `_ssh_host` → Bridge `/restart` ist symmetrisch zum bestehenden cli-bridge-Propagationspfad
  (ADR-054) und braucht kein neues Polling-Intervall.

## Konsequenzen

### Positiv

- **Generisches Muster.** Der nächste Host-Harness (Claude Code mit custom Endpoint, oder ein
  drittes CLI) bekommt einen Adapter statt eines neuen Hardcode-Branches in drei verschiedenen
  Dateien.
- **Hermes folgt jetzt tatsächlich der gebundenen Runtime** — Modellwechsel auf Spark erreichen
  Hermes automatisch (Watcher-Tick, ≤90s) statt für immer auf `ollama-cloud` festzuhängen.
- **Bidirektionaler Switch.** Hermes kann jederzeit zwischen kompatiblen Runtimes umgeschaltet
  werden (Spark ↔ ollama-cloud ↔ jede weitere openai-kompatible Runtime), ohne die
  Single-Instance-Garantie zu verletzen.
- **`single_instance` bedeutet wieder, was es sagen sollte** — kein Kollateralschaden für den
  In-Place-Fall, der eigentliche Parallel-Instanz-Schutz bleibt hart.
- **Kein neuer Infrastruktur-Baustein.** Kein LiteLLM-Prozess, kein zusätzlicher Proxy — reine
  Backend-Service-Schicht + ein Config-Patch-Script, das ohnehin schon existierte.

### Negativ

- **Adapter-Registry ist noch klein (1 Eintrag).** Das generische Muster ist bislang nur an einem
  Fall (Hermes) verifiziert — ob das Interface für einen zweiten, strukturell anderen Host-Harness
  (z. B. mit eigenem Approval-Flow) tatsächlich ausreicht, zeigt sich erst beim nächsten Adapter.
- **`ClaudeCodeHostAdapter` ist reine Dokumentation.** Kein Code, kein Test — die Design-Annahmen
  (OAuth-Keychain-Ausnahme, `launchctl kickstart`-Reload) sind unverifiziert bis zur tatsächlichen
  Implementierung.
- **Zwei Config-Quellen für Hermes bleiben nebeneinander bestehen** (`agent.env` für MC-interne
  Zwecke + `~/.hermes/config.yaml` für das Binary). Der Patch-Schritt ist ein zusätzlicher
  Übersetzungs-Hop, den man bei einem hypothetischen Hermes-Redesign (native `OPENAI_*`-Unterstützung)
  wieder entfernen könnte.
- **Kein neuer LiteLLM-Shim heisst weiterhin: kein Cross-Protocol-Switch.** Wer Claude Code gegen ein
  lokales OpenAI-Modell laufen lassen will, hat auch nach diesem Change keinen Weg dorthin — bewusst
  ausserhalb des Scopes, aber eine bekannte Grenze.

## Implementierung

- `backend/app/services/host_harness_adapter.py` — NEU: `HostHarnessAdapter`-Protocol, `HermesAdapter`,
  `HOST_ADAPTERS`-Registry, `get_adapter()`, `sync_host_agent_model()`.
- `backend/app/routers/agents.py` (~1489) — Provisioning-Dispatch: `get_adapter(harness)` +
  `is_compatible()`-Gate statt `if runtime.runtime_type == "hermes"`-Branch (400 unbekannt / 422
  inkompatibel).
- `backend/app/services/runtime_propagation.py` — `mark_agents_for_sync` flaggt Host-Agents mit
  Adapter (Zeile ~58–65); `_sync_one`-Äquivalent verzweigt host (`sync_host_agent_model` +
  `adapter.reload()`) vs. cli-bridge (Zeile ~112–131).
- `backend/app/services/agent_runtime_switch.py` — `_is_host_inplace(agent)` (Zeile ~299–310); host
  In-Place-Switch-Branch (Zeile ~524–595); `single_instance`-Checks jetzt `if not is_host_inplace`
  (Zeile ~425–449).
- `scripts/hermes-config-patch.py` — `_model_patches_from_env()`: `model.provider/base_url/default`
  aus `OPENAI_BASE_URL`/`OPENAI_MODEL`, Guard bei fehlenden Vars, idempotent.
- `docker/hermes/entrypoint.sh` — ruft den Patcher nach `source agent.env` und vor dem Hermes-Start
  auf, bei jedem (Re-)Start.
- `backend/config/runtimes.json` — `ollama-cloud`-Eintrag (`runtime_type: cloud`, openai-kompatibel)
  als reguläres Switch-Ziel.
- `frontend-v2/src/components/shared/RuntimeSwitchModal.tsx` + `frontend-v2/src/app/agents/[id]/page.tsx`
  + `frontend-v2/src/app/agents/page.tsx` + `frontend-v2/src/lib/types.ts` — Runtime-Picker + Reload-Button
  für host-Agents mit Adapter; `RuntimeSwitchModal` zeigt In-Place-Hinweis statt Hard-Lock.
- Commits: `7f5127fd`..`7b669f4f` auf `feat/host-harness-adapter`.

## Verwandte ADRs

- **ADR-029** (Hermes als host-side tmux Worker) — Amendment: `single_instance` bedeutet „keine
  parallele Instanz", nicht „kein Umbinden"; In-Place-Switch ist die hier ergänzte, sichere Form des
  Umbindens.
- **ADR-054** (Runtime Watcher — model-drift auto-detection) — der Auto-Forward-Mechanismus, den
  Host-Agents jetzt erben (`mark_agents_for_sync`/Propagation-Tick, Circuit-Breaker-Pattern).
- **ADR-056** (Harness/Provider-Decoupling) — `harness_compat.is_compatible()`/`incompat_reason()`,
  die dieser Change auf den Host-Provisioning-Einstieg und den In-Place-Switch ausweitet;
  `protocol`-Feld des Adapters spiegelt die dortige Compat-Matrix.
- **ADR-048** (Host-Registry) — orthogonal: die physische Maschine, auf der eine Runtime läuft
  (`hosts`-Tabelle), bleibt unverändert; dieser ADR betrifft die Bindung Host-**Agent** ↔ Runtime, nicht
  Runtime ↔ physischer Host.
- **ADR-045/049** (omp Runtime, native TUI) — Vorbild für „MC folgt der Engine automatisch" auf einem
  anderen Harness; Hermes bekommt hier das äquivalente Muster für den Host-Fall.
