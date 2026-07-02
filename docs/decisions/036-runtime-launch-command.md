# ADR-036 — Runtime `launch_command` für recipe-launched Container

**Status:** Accepted
**Datum:** 2026-05-15
**Scope:** Backend/Runtime, Infra/Runtime
**Erweitert:** ADR-028 (Runtime Registry DB-only)

## Kontext

ADR-028 (Phase 16) hat die Runtime-Registry DB-only gemacht: `runtimes`-
Tabelle ist Quelle der Wahrheit, `runtime_manager.start_runtime(rt)` macht
für `vllm_docker`-Runtimes simpel `docker start <container_name>` via SSH.

Das Modell scheiterte am 2026-05-16 wieder: Der Operator hat den qwen-vllm-Container
`sparkrun_1299888bb0f6_solo` über die `/runtimes` UI gestoppt. `docker stop`
durch sparkrun's `--rm` Default entfernte den Container vollständig statt
ihn nur anzuhalten. Folge-Click auf "Start" rief
`docker start sparkrun_1299888bb0f6_solo` auf einem nicht mehr existierenden
Container auf — `Error: No such container`.

Das Problem ist nicht spezifisch für sparkrun: jeder recipe-basierte
Launcher (k3s-Pods, systemd-units mit auto-cleanup, ephemere LM-Studio-
Workloads) macht ähnliches. Die ADR-028-Annahme "ein Container-Name
identifiziert dauerhaft denselben Container" hält nicht.

## Entscheidung

Neues nullable Feld `runtimes.launch_command` (TEXT) als Recipe-Aware
Re-Launch-Pfad. `start_runtime()` für `vllm_docker` entscheidet zur
Aufrufzeit:

```
Path A: docker inspect --format '{{.State.Status}}' <container_name>
        → Container existiert  → docker start <container_name>
Path B: Container nicht da     → SSH `bash -lc <launch_command>` (detached
                                  via nohup, Logs nach
                                  ~/.cache/mc/runtime-launch-<slug>.log)
Path C: Container nicht da +
        keine launch_command   → klare Fehlermeldung
```

Path B nutzt `shlex.quote()` damit ein UI-pasted launch_command mit `;` oder
`$(…)` als **ein** Argument an `bash -lc` geht und nicht von der SSH-host-
Shell expanded wird.

Die Recipe ist verantwortlich, den resultierenden Container zu **labeln**
(z.B. `--label mc.runtime.slug=qwen-general`), damit zukünftige
`stop_runtime()` / `restart_runtime()` Calls den Container weiterhin per
Label finden können — ob die Recipe einen deterministischen Namen vergibt
(sparkrun's hash-basierter `sparkrun_<hash>_solo`) oder einen frischen,
ist für MC dann opaque.

Migration `0117_runtime_launch_command.py` legt die Spalte (nullable) an.
Migration `0118_seed_qwen_general_launch_command.py` seedt qwen-general
mit dem live-verifizierten sparkrun-Aufruf:

```
uvx sparkrun run @official/qwen3.6-35b-a3b-fp8-vllm
  --solo --no-rm --ensure --no-follow
  --label mc.runtime.slug=qwen-general
```

Idempotent (`WHERE launch_command IS NULL`), so dass hand-edits später
nicht überschrieben werden.

## Alternativen

- **Container-Name als stable label-based identifier** (`docker ps -q
  --filter label=mc.runtime.slug=…`) statt `container_name`-Spalte →
  Verworfen jetzt (zu invasiver Refactor: stop/restart/health/probe
  müssten alle parallel umgestellt werden, plus die Frontend-UI editiert
  noch `container_name`). Möglich als Phase 2 wenn launch-Pfad sich
  bewährt.
- **Recipe-Spalte statt `launch_command`** mit Backend-side template
  ("für sparkrun: `uvx sparkrun run @<recipe> --solo …`") → Verworfen
  weil das MC zu einem Wrapper-für-sparkrun macht. `launch_command` als
  freie Shell-Zeile bleibt vendor-agnostisch.
- **Auto-Recreate aus Image-Tag** (`docker run --rm <runtime.image>`) →
  Verworfen weil sparkrun den Container mit zig Args hochfährt
  (model, gpu-mem, kv-cache, etc.) die wir nicht aus dem Image-Tag
  ableiten können.
- **Backend lockt sparkrun's CLI ab** (`subprocess.Popen(["uvx",
  "sparkrun", …])`) → Verworfen weil `sparkrun` auf der DGX-Spark läuft,
  nicht im Backend-Container — SSH ist zwingend.

## Konsequenzen

### Positiv

- `/runtimes` Start klappt wieder, auch nach `--rm`-induziertem Cleanup
  (zwei Klicks: stop → start, kein manueller SSH-Eingriff nötig).
- Vendor-agnostisch: derselbe Pfad funktioniert für sparkrun, k3s
  apply-Befehle, systemd-unit start, custom Bash-Scripte.
- Idempotenz auf zwei Ebenen: `--ensure` im sparkrun-Aufruf macht den
  Launch idempotent, `WHERE launch_command IS NULL` in der Seed-Migration
  schützt manual edits.
- `shlex.quote()` schliesst Shell-Injection auch wenn der Operator einen
  Launch-Command mit `&&` oder `;` einträgt.

### Negativ

- `container_name` und `launch_command` müssen konsistent gehalten werden:
  wenn die Recipe einen anderen Containernamen erzeugt als in der DB,
  läuft beim nächsten Start Path A leer und Path B rennt → ggf. zwei
  Container parallel. Heute kein Problem (sparkrun-hash ist
  deterministisch), aber eine spätere Recipe-Änderung könnte das brechen.
- `stop_runtime()` und `restart_runtime()` nutzen weiter `container_name`
  — wenn Path B einen frischen Namen erzeugt hat, ist DB stale. Migration
  zu label-based stop ist als Phase 2 markiert, aber bewusst NICHT in
  diesem Commit.
- Frontend RuntimeEditor zeigt das Feld noch nicht — Operator-Edits müssen
  per `PATCH /api/v1/runtimes/{id}` o.ä. direkt gegen die API laufen.
  Separate Frontend-Phase steht aus.
- `nohup … &` läuft detached: Backend bekommt vom anschliessenden
  Boot-Prozess kein Feedback. State wird über `health` / Probe-URL
  gepullt, nicht gepushed.

## Referenzen

- Betroffene Dateien:
  - `backend/app/models/runtime.py:34-43` (`launch_command` Field)
  - `backend/app/services/runtime_manager.py:420-475` (`start_runtime`
    Path-A/B/C)
  - `backend/app/routers/runtimes.py:60, 84` (RuntimeCreate/Update Schemas)
  - `backend/alembic/versions/0117_runtime_launch_command.py`
  - `backend/alembic/versions/0118_seed_qwen_general_launch_command.py`
- Commits: `f3d24918` — feat(runtimes): launch_command fallback when
  container is gone, `932e4999` — chore(seed): qwen-general launch_command
- Verwandte ADRs: ADR-028 (Runtime Registry DB-only), ADR-027 (Universal
  Agent Runtime Binding), ADR-013 (Settings.json Single SoT)
- Tests: `backend/tests/test_runtime_launch_command.py` (8 Cases)
- Live-verifiziert: 2026-05-16 12:00 UTC, qwen-general start → vllm
  HTTP 200 nach 5 min Build + 3 min Warmup; Researcher-Task "Wetter" lief
  in 82 s mit qwen3.6 (44× schneller als Nemotron, siehe
  `project_mc_runtime_glm51_fix_2026-05-15.md` Memory).
