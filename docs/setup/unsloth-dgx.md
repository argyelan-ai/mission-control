# Unsloth Studio auf DGX Spark (oder vergleichbarem ARM64-Host)

Unsloth Studio ist das offizielle Web-UI für Inference + Fine-Tuning auf Basis von [unslothai/unsloth](https://github.com/unslothai/unsloth). MC kann das Studio als vierten Runtime-Typ ansteuern (start/stop via SSH + tmux, health via HTTP-probe).

Das offizielle Unsloth-Docker-Image ist **amd64-only**. Auf einem DGX Spark (aarch64) muss der native Installer verwendet werden.

## Vorbedingungen

- SSH-Zugang zum DGX als User mit passwordless-key (siehe `DGX_SSH_USER` in `.env`)
- Python ≥ 3.10 auf dem DGX
- Docker ist verfügbar (andere Runtimes wie vLLM laufen dort eh)
- Host-Seitige Packages: `sudo apt-get install -y cmake build-essential libcurl4-openssl-dev pciutils`

## Installation

```bash
# Auf dem DGX Spark
cd ~
git clone https://github.com/unslothai/unsloth.git unsloth-studio
cd unsloth-studio
./install.sh --local
```

Der Installer legt ein Python-Virtualenv unter `~/unsloth_studio` an und installiert PyTorch + Unsloth + Studio-Dependencies. Dauer: 5–20 Minuten je nach Netzwerk.

## Start (manuell zum Testen)

```bash
cd ~
~/unsloth_studio/bin/unsloth studio -H 0.0.0.0 -p 8888
```

Das Studio ist jetzt unter `http://<dgx-ip>:8888` erreichbar.

## Start (über MC)

1. In `backend/config/runtimes.json` ist der Seed-Eintrag `unsloth-studio` bereits enthalten (disabled).
2. Nach dem nächsten Backend-Start (Seeder läuft idempotent) erscheint die Runtime im UI unter `/runtimes`.
3. Runtime aktivieren: PATCH via UI oder direkt SQL:
   ```sql
   UPDATE runtimes SET enabled = TRUE WHERE slug = 'unsloth-studio';
   ```
4. Auf der Runtimes-Seite auf **Start** klicken. MC führt per SSH
   ```
   tmux new-session -d -s unsloth-studio 'cd ~ && unsloth studio -H 0.0.0.0 -p 8888'
   ```
   aus. Nach 1–3 Minuten geht der Runtime auf `ready`.

## Agenten auf Unsloth routen

Agent-Detail → Runtime-Dropdown → `Unsloth Studio (DGX)` wählen → **Apply & Restart**. Beim nächsten openclaude-Start zeigt der Agent auf `http://<dgx-ip>:8888`.

> Hinweis: Unsloth Studio muss ein Modell geladen haben, bevor Agenten es nutzen können. Das geschieht via Unsloth Studio UI direkt — nicht via MC. MC steuert nur das Studio-Lifecycle (start/stop), nicht welches Modell geladen ist.

## Troubleshooting

- **`tmux has-session` schlägt fehl:** SSH-Key falsch? Teste manuell: `ssh <dgx-user>@<dgx> "tmux ls"`.
- **HTTP-Probe schlägt fehl, State bleibt `warming`:** Unsloth Studio braucht beim ersten Start Modell-Download. Logs: `ssh <dgx-user>@<dgx> "tmux capture-pane -t unsloth-studio -p | tail -60"`.
- **Port 8888 belegt:** ein anderes Tool (oft Jupyter) hält den Port. `runtimes.json` → `endpoint` + `startup-command` auf einen freien Port umstellen (z. B. 8889), Runtime-Row via `PATCH /api/v1/runtimes/db/unsloth-studio` aktualisieren.
- **Installer fragt nach sudo:** die in den Vorbedingungen genannten `apt-get install`-Packages fehlen. Einmal nachinstallieren, dann `./install.sh --local` erneut starten.

## Entfernen

```bash
# Auf dem DGX
tmux kill-session -t unsloth-studio 2>/dev/null
rm -rf ~/unsloth-studio ~/unsloth_studio
```

In MC: `DELETE /api/v1/runtimes/db/unsloth-studio`.
