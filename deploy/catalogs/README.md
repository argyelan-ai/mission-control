# App-Store / Catalog Manifests

Vorbereitete Manifeste, um MC in Self-Hosting-Kataloge zu bringen —
**Distribution**: dort suchen Homelab-Nutzer nach Apps.

| Katalog | Datei | Einreichung |
|---|---|---|
| **Portainer** | `portainer-template.json` | Kein zentraler Store: Nutzer fuegen die Raw-URL dieses Files als App-Template-Quelle hinzu (Settings → App Templates). URL im README bewerben. |
| **CasaOS** | `casaos-app.yml` | PR an [IceWhaleTech/CasaOS-AppStore](https://github.com/IceWhaleTech/CasaOS-AppStore) (Ordner `Apps/mission-control/` mit docker-compose.yml + Icons). |
| **Umbrel** | — | PR an [getumbrel/umbrel-apps](https://github.com/getumbrel/umbrel-apps): `umbrel-app.yml` + eigenes compose. Manifest analog zu CasaOS; anlegen, sobald die GHCR-Images live sind (Umbrel verlangt gepinnte Digests). |
| **Runtipi** | — | PR an [runtipi/runtipi-appstore](https://github.com/runtipi/runtipi-appstore); `config.json` + compose. Ebenfalls nach GHCR-Go-Live. |

**Voraussetzung fuer alle:** die GHCR-Images (`.github/workflows/release.yml`)
muessen mindestens einmal publiziert sein — Kataloge bauen nicht lokal.

**Wichtig bei Einreichungen:** Die Kataloge koennen kein `setup.sh` ausfuehren.
Die Manifeste deklarieren Secrets als Pflicht-Env-Vars, die der Store-Nutzer
beim Installieren setzt (CasaOS/Umbrel generieren teils selbst Zufallswerte).
Der Funktionsumfang im Katalog-Deployment ist der Kern-Stack ohne
Agent-Fleet-Extras (Host-Mounts) — fuer die volle Erfahrung verlinken die
Beschreibungen auf den Quickstart.
