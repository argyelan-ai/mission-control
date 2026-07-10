# ADR-065 — X (Twitter) Publisher via Draft -> Approve -> Post

**Status:** Accepted
**Datum:** 2026-07-10
**Scope:** Backend/Content · Backend/Auth

**Numbering note:** highest ADR at branch time was 063. Picked 065 (skipping 064)
in case a parallel branch also claims 064 — check `docs/decisions/` for a
064 file at merge time and renumber if there's a collision.

## Kontext

Mark (X Premium+) will halbautomatisch auf X posten: ein Agent oder ein
System-Prozess schlaegt einen Tweet-Text vor, Mark approved ihn, Mission
Control postet ueber die X API v2. Automatisierte Replies sind API-seitig
verboten und daher explizit nicht Teil dieses Scopes.

MC hat bereits zwei Lifecycles, die dafuer in Frage kamen:

- **Approval** (`approvals` Tabelle + `resolve_approval()`) — der generische
  Operator-Freigabe-Mechanismus mit einem `action_type`-Dispatch fuer
  Post-Resolve-Hooks (install_skill, spawn_agent, blocker_decision, ...).
- **ContentPipeline** (`content_pipelines` Tabelle) — Multi-Stage
  Content-Lifecycle (idea -> ... -> approved -> published) mit
  `published_url` / `published_platform` / `published_at` Feldern, die
  `twitter` bereits als moeglichen Wert kennen.

Zusaetzlich existiert im maintainer-privaten `news_studio`-Vertical (aus dem
public OSS-Repo herausgeloest, siehe Commit "extract news-studio into a
strippable vertical module" — gitignored, nicht Teil dieses Branches/Worktrees)
bereits ein `publish_twitter()` in `services/publish_adapters.py`. Der ist
Thread-foermig (mehrere Tweets, getrennt durch Leerzeilen), an Storyboards
gebunden und nutzt einen einzelnen `twitter_bearer_token` Secret — das reicht
fuer echtes User-Context-Posten ueber die X API v2 nicht aus (dafuer braucht
es OAuth 1.0a: Consumer Key/Secret + Access Token/Secret). Er ist aus diesem
Grund und weil er vertical-spezifisch/nicht im OSS-Kern ist, keine passende
Basis fuer einen generischen, immer verfuegbaren Single-Post-Flow.

## Entscheidung

**Kein dritter Lifecycle.** Der neue `x_publisher`-Service haengt sich in die
zwei bestehenden Lifecycles ein statt einen eigenen (z.B. eine
`XPost`-Tabelle) zu bauen:

1. **Draft-Erstellung:** `POST /api/v1/agent/x-posts` (Scope `content:submit`)
   validiert den Draft (<=280 Zeichen, Link-Kosten-Hinweis) und legt eine
   `Approval(action_type="x_post")` an — Payload traegt `text` +
   optional `content_pipeline_id` + `requester_task_id`. Idempotent wie
   `install-requests`: identischer pending Draft liefert dieselbe
   `approval_id` zurueck (200 statt 201).
2. **Approve/Reject:** normale `PATCH /api/v1/approvals/{id}` UI/API — kein
   neuer Endpoint, keine neue UI noetig fuer v1.
3. **Post-Resolve-Hook** (`_handle_x_post_resolution` in
   `routers/approvals.py`): bei `approved` ruft `x_publisher.post_text()`
   (tweepy, OAuth 1.0a User-Context) auf. Ergebnis landet in
   `approval.resolver_note` (Tweet-URL oder Fehler-Klassifikation) +
   `activity_event` (`x_post.published` / `x_post.failed`). Wenn ein
   `content_pipeline_id` mitgeschickt wurde, wird die verlinkte
   `ContentPipeline`-Row aktualisiert (`published_url`,
   `published_platform="twitter"`, `published_at`, `status="published"`) —
   das ist der bestehende Content-Lifecycle, nicht ein neuer.
4. **Secrets:** 4 System-Tokens in der `secrets`-Tabelle (ADR-033:
   "wie MC selbst mit der Welt redet" statt Task-Vault `credentials`) —
   `x_api_key`, `x_api_secret`, `x_access_token`, `x_access_token_secret`.
   Fehlen sie, liefert `post_text()` einen sauberen `missing_secrets`-Fehler
   statt zu crashen.

## Alternativen

- **Neue `x_posts`-Tabelle mit eigenem Status-Feld** (pending/approved/posted).
  Verworfen: haerte genau das Muster, das die "no second lifecycle"-Regel des
  Projekts verhindern soll — Approval deckt "wartet auf Operator-Entscheidung"
  bereits vollstaendig ab, inkl. SSE, Telegram-Quick-Resolve-Infrastruktur
  (Telegram-Pfad fuer x_post ist v1 noch nicht verdrahtet, siehe Report/Follow-up).
- **`NewsPostSchedule` (aus `models/news.py`) wiederverwenden.** Verworfen:
  das Model ist an `NewsArticle` gekoppelt (FK `article_id`) und Teil des
  News-Vertical-Datenmodells — ein generischer Agent-Draft ohne Artikel-Bezug
  passt schlecht rein, ausserdem ist die zugehoerige Verarbeitung Teil des
  privaten news_studio-Verticals.
- **Bestehenden `publish_twitter()` in `publish_adapters.py` erweitern.**
  Verworfen: liegt im gitignorten, maintainer-privaten Vertical und ist damit
  in diesem Worktree/Branch gar nicht vorhanden; ausserdem Thread-Post-shaped
  und mit dem falschen Auth-Modell (Bearer statt OAuth 1.0a) fuer
  User-Context-Posts.

## Konsequenzen

### Positiv
- Ein Freigabe-Mechanismus im ganzen Projekt — Mark muss keinen zweiten
  Ort lernen, um X-Posts zu approven.
- `ContentPipeline` bleibt die einzige Quelle der Wahrheit fuer
  "was wurde wo published" — kein Doppel-Tracking.
- Sauberes Error-Handling: Rate-Limit (429), Forbidden/Duplicate (403),
  Unauthorized (401), fehlende Secrets — alles klassifizierte Result-Objekte,
  nie ein Crash des Approval-Resolve-Endpunkts.

### Negativ
- `Approval.payload` ist ein loses JSON-Blob ohne eigenes Schema — fuer
  komplexere Post-Typen (Media-Upload, Threads) muesste das Payload-Schema
  wachsen oder ein eigenes Model doch noetig werden. Fuer v1 (Text-only,
  <=280 Zeichen) reicht das JSON-Blob.
- Kein Telegram-Quick-Resolve fuer `x_post` verdrahtet (nur der
  User-JWT-Pfad `PATCH /api/v1/approvals/{id}`) — Follow-up falls Mark
  X-Posts vom Handy aus per Telegram-Link freigeben will.
- Keine Frontend-Aenderung in v1 — die Approvals-Inbox zeigt `x_post` wie
  jeden anderen Approval-Typ generisch an (Description-Text), ohne
  X-spezifisches Preview/Charcount-UI. Follow-up falls gewuenscht.

## Referenzen

- `backend/app/services/x_publisher.py` — tweepy-Client, Validation, Error-Klassifikation
- `backend/app/routers/x_posts.py` — Draft-Erstellung (mirrors `install_requests.py`)
- `backend/app/routers/approvals.py` — `_handle_x_post_resolution()` Hook
- `backend/tests/test_x_publisher.py`, `test_x_posts_endpoint.py`, `test_x_post_approval_flow.py`
- [ADR-033](033-secrets-vs-credentials-boundary.md) — Secrets vs Credentials Boundary
- [ADR-015](015-install-approval-flow.md) — Install-Approval Flow (Vorbild fuer den Hook-Dispatch-Stil)
