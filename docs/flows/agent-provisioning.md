# Agent Provisioning

> Wie ein Agent erstellt, auf dem Gateway provisioniert und konfiguriert wird.

## Uebersicht

```
Template/Custom → DB Agent → Token generieren → Gateway Provisioning → Config Sync → Einsatzbereit
```

## 1. Agent-Erstellung

### Via Template (User)
```
routers/agent_templates.py:instantiate_template()
  → POST /agent-templates/{template_id}/instantiate
  → _do_instantiate():
      → Template laden (soul_md, skills, role, model, emoji)
      → Agent INSERT (provision_status="local")
      → generate_agent_token() → PBKDF2-SHA256 Hash → agent_token_hash
      → Token wird einmalig im Response zurueckgegeben (danach nie mehr sichtbar!)
      → _generate_tools_md(agent) → TOOLS.md mit API-Endpoints
      → emit_event("agent.created")
```

### Via Specialized Templates
```
routers/agents.py:instantiate_specialized()
  → POST /agents/specialized/{spec_key}/instantiate
  → spec_key: planner, researcher, writer, reviewer
  → Findet passendes Builtin-Template → _do_instantiate()
```

### Via Agent (Board Lead)
```
routers/agent_scoped.py:agent_create_agent()
  → POST /agent/agents
  → Nur Board Lead darf Agents erstellen
  → _do_instantiate() oder Custom-Creation
  → Optional: _provision_agent_background()
```

### Custom Agent
```
routers/agents.py:create_agent()
  → POST /agents
  → Agent INSERT mit manuellen Feldern
  → generate_agent_token()
  → _generate_tools_md()
```

## 2. Token-Generierung

```python
# In routers/agents.py oder agent_templates.py
raw_token = secrets.token_urlsafe(32)       # 256-bit random
salt = os.urandom(16)
hash = hashlib.pbkdf2_hmac("sha256", raw_token, salt, 200_000)
agent.agent_token_hash = f"{salt.hex()}:{hash.hex()}"
# raw_token wird NUR EINMAL im Response zurueckgegeben
```

**Token Reset:**
```
routers/agents.py:reset_agent_token()
  → POST /agents/{agent_id}/reset-token
  → Neuen Token generieren, alten Hash ueberschreiben
  → Einmalig neuen Token zurueckgeben
```

## 3. Gateway Provisioning

```
routers/agents.py:provision_agent()
  → POST /agents/{agent_id}/provision
  → BackgroundTask: _provision_agent_background()
      → provision_status = "provisioning"
      → _cleanup_sync_ghosts() — verwaiste Sync-Eintraege loeschen
      → Gateway Agent erstellen:
          → agents_update(gateway_agent_id, ...) ODER neuen Agent via RPC
          → Workspace auf Gateway einrichten
          → Model-Format konvertieren: _convert_model_to_oc_format()
      → Config-Dateien schreiben:
          → SOUL.md (Identitaet + Rolle)
          → TOOLS.md (API-Endpoints, generiert via _generate_tools_md)
          → HEARTBEAT.md (Heartbeat-Konfiguration)
      → provision_status = "provisioned"
      → provisioned_at = now()
      → emit_event("agent.provisioned")
```

## 4. Config Sync

### TOOLS.md Generierung
```
_generate_tools_md(agent):
  → MC Backend URL (aus Settings)
  → Agent Token (Bearer Auth)
  → Verfuegbare Endpoints:
      - GET /agent/boards/{board_id} — Board Snapshot
      - POST/PATCH /agent/boards/{board_id}/tasks/* — Task Management
      - POST /agent/boards/{board_id}/memory — Memory erstellen
      - POST /agent/boards/{board_id}/approvals — Genehmigung anfragen
      - POST /agent/boards/{board_id}/chat — Chat-Nachricht
      - GET/POST /agent/knowledge — Knowledge Base
      - POST /agent/content/{pipeline_id}/submit — Content einreichen
```

### Config Update
```
routers/agents.py:update_agent_config()
  → PATCH /agents/{agent_id}/config/{file_type}
  → file_type: soul_md, tools_md, heartbeat_md, rules_md, identity_md
  → DB Update
  → Optional: sync_to_gateway=true
      → rpc.agents_files_set(gateway_agent_id, file_type, content)
```

### Bulk Config Sync
```
routers/agents.py:sync_agent_config()
  → POST /agents/{agent_id}/sync-config
  → Alle Config-Dateien (soul_md, tools_md, heartbeat_md) auf Gateway pushen
```

## 5. Discord Integration

```
routers/agents.py:create_discord_channel()
  → POST /agents/{agent_id}/discord-channel
  → gateway_client.create_discord_channel(name, context, category_id)
  → Agent.discord_channel_id + discord_channel_name speichern
  → gateway_client.bind_agent_channel(gateway_agent_id, channel_id)
```

## 6. Gateway Sync (Rueckrichtung)

```
services/gateway_sync.py:sync_agents_from_gateway()
  → POST /gateways/openclaw/sync
  → Alle Agents vom Gateway laden (rpc.agents_list)
  → Match-Logik:
      1. gateway_agent_id → direkte Zuordnung
      2. Name-Fallback (nur fuer Agents ohne gateway_agent_id)
  → Sync-Richtung:
      → OC → MC: model, status (Gateway ist Master)
      → MC → OC: board_id, soul_md, tools_md (MC ist Master)
  → Duplikat-Handling: Board-assigned Agents bevorzugt
```

## Provision-Status Flow

```
local → provisioning → provisioned
                    ↘ error
```

- `local`: Agent existiert nur in MC DB
- `provisioning`: Background-Task laeuft
- `provisioned`: Workspace + Configs auf Gateway bereit
- `error`: Provisioning fehlgeschlagen

## Side-Effects

| Aktion | DB | Redis | Gateway | Discord |
|--------|----|----|---------|---------|
| Agent erstellt | Agent + Token | - | - | - |
| Provisioning | Agent UPDATE | - | Workspace + Files | - |
| Config Sync | Agent UPDATE | - | files_set() | - |
| Discord Channel | Agent UPDATE | - | bind_agent_channel() | Channel erstellt |
| Gateway Sync | Agent UPDATE (bulk) | - | agents_list() | - |

## Edge Cases

- **Ghost Agents**: `_cleanup_sync_ghosts()` raeumt verwaiste Gateway-Sync-Eintraege auf
- **Model Format**: OpenClaw nutzt `provider/model-name`, MC speichert manchmal anders → `_convert_model_to_oc_format()`
- **Token Sicherheit**: Token nur einmal sichtbar, danach nur Hash gespeichert. Kein Recovery moeglich.
- **Duplikat bei Sync**: Board-assigned Agents haben Vorrang bei `by_gw_id` Index
