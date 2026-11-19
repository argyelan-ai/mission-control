# ADR-010 — Redis-Cache für PBKDF2 Agent-Token-Verify

**Status:** Accepted
**Datum:** 2025 (bei Agent-Auth Feature)
**Scope:** Backend/Auth

## Kontext

Agent-Tokens werden in DB als **PBKDF2-SHA256 mit 200 000 Iterationen** gespeichert (Salt + Hash). Bei jedem Agent-Request muss der Token verifiziert werden.

**Problem**: Ein PBKDF2-Verify mit 200k iterations dauert ~200ms auf dem Mac Mini M4. Bei 10 Agents, jeder pollt alle 5 Sekunden + postet Heartbeat + postet Kommentare → **easily 20+ Requests/s**. Ohne Cache würden alle ~200ms blockieren = **~4s CPU pro Sekunde** nur für Token-Verify.

## Entscheidung

**Redis-Cache** mit folgendem Key-Schema:
```
Key:   mc:agent:token:{SHA256(token)}
Value: {agent_id}
TTL:   300 Sekunden (5 Minuten)
```

Flow bei Agent-Request:
1. SHA256(token) berechnen (< 1ms)
2. Redis-Lookup — wenn hit → agent_id direkt aus Cache, DB-Get für Agent-Object, done
3. Wenn miss → DB-Query aller Agents → für jeden PBKDF2-Verify → bei Match: in Redis speichern, return agent

**Sicherheits-Design**:
- Wir speichern `SHA256(token)`, **nie das Token selbst** (auch nicht gehashed in Redis)
- Cache-Value ist nur die `agent_id`, nicht der ganze Agent
- TTL 5min: Agent-Session hält länger als 5min, Cache-Hits dominieren
- Bei Token-Reset: alte Cache-Einträge laufen einfach ab (kein explizites Invalidate nötig, weil Token-Reset selten ist)

## Alternativen

- **A: Kein Cache, jedes Request PBKDF2-Verify** → Performance-GAU, siehe Kontext
- **B: In-Memory-Cache pro Worker** → verworfen weil:
  - Multi-Worker-Deployment würde Cache-Duplikate haben
  - Worker-Restart = kalter Cache
  - Redis sowieso schon im Stack
- **C: Symmetric Encryption statt PBKDF2** → verworfen weil Token-Compromise = alle Tokens compromised
- **D: JWT für Agents** → verworfen weil:
  - Rotation schwieriger (JWT-Revoke braucht zusätzlichen Mechanism)
  - User-JWT ist eh schon HS256, aber für Agents wollten wir Token-basierte Auth (einfacher in Curl-Befehlen, kein JWT-Library nötig im Agent)
- **E: BCrypt statt PBKDF2** → funktional äquivalent, nicht getestet, kein grosser Gewinn

## Konsequenzen

### Positiv
- **Performance**: 5ms statt 200ms pro Request (40x schneller)
- **Skalierbarkeit**: Viele Agents + hohe Polling-Frequenz möglich ohne CPU-Kollaps
- **Sicherheit bleibt intakt**: Token-Hash nur in DB, Cache enthält nur SHA256(token) → agent_id Mapping
- **Simple Implementation**: ~30 Zeilen in `auth.py`, standard Redis-Calls
- **Graceful Degradation**: Redis down → Fallback zu DB+PBKDF2 (langsamer aber funktional)

### Negativ
- **5min Token-Invalidation-Lag**: Nach Token-Reset hat alter Cache-Eintrag bis zu 5min Gültigkeit — **Trade-off akzeptabel** (Token-Resets sind selten, zusätzlich würde explizites Invalidate komplexer) — *superseded 2026-11-13, see Nachtrag: a reset now takes effect at once.*
- **Redis-Abhängigkeit**: Wenn Redis down, Performance bricht ein (aber System funktioniert)
- **Cache-Miss-Storm möglich**: Wenn viele Agents gleichzeitig nach Redis-Restart pollen, alle DB+PBKDF2 gleichzeitig → potential für CPU-Spike (bisher nicht beobachtet)

## Referenzen

- Code: `backend/app/auth.py` (`verify_agent_token()`, `_cache_agent_token()`)
- Redis-Keys: `backend/app/redis_client.py` (`RedisKeys` Helper)
- Verwandt: ADR-009 (Agent-Scoped Router), [OWASP Password Storage](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)

## Nachtrag (2026-11-13) — a replaced token is refused at once

The decision above accepted that a cache entry of a replaced token "simply
expires" — up to five minutes in which the **old** token still worked. That
trade-off is withdrawn. Resetting the token is the only way to cut an agent's
access for good (ADR-101 names it as the way back for a worker on a machine
MC does not manage), so "reset" has to mean "gone now".

**What changed.** The cache value is no longer the agent id alone:

```
Key:   mc:agent-auth:{SHA256(token)}
Value: {agent_id}:{first 32 hex chars of SHA256(stored token hash)}
TTL:   300 seconds (unchanged)
```

A cache hit counts only while the agent's stored hash still produces the same
fingerprint. After a reset (or any other path that replaces the hash —
re-provision, bootstrap, terminal re-key) the entry no longer matches: it is
deleted and the token is verified with PBKDF2 again, which refuses it.

- **No explicit invalidation.** The reset route does not know the old raw
  token and so cannot name its cache key; binding the entry to the hash needs
  no knowledge of the old token and covers every path that replaces a hash,
  including ones added later.
- **Cost:** none on the hot path. A hit already loaded the agent row; the
  check adds one SHA256 over a short string. PBKDF2 still runs once per token
  per five minutes.
- **What Redis holds:** still neither the token nor the stored hash — only a
  one-way fingerprint of the stored hash.
- **Entries in the old format** (agent id only) fail the check and are
  verified again once. No migration, no flush at deploy.
- **Not changed:** a *paused* agent's token keeps working on the agent
  routes; pausing stops the delivery of cards, not the access.

The key name in "Entscheidung" above (`mc:agent:token:…`) and the function
names in "Referenzen" were never the ones in the code. The code is
`_agent_token_cache_key`, `_agent_token_cache_value` and
`_resolve_agent_from_token` in `backend/app/auth.py`; the guard is
`backend/tests/test_agent_token_cache_revocation.py`.
