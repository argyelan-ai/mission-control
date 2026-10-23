# ADR-098 — Credentials Vault admin-only, masks without length or password characters

**Status:** Accepted
**Datum:** 2026-10-23
**Scope:** Backend/Auth · Backend/API · Frontend/Settings · Security

## Kontext

Round-3 user tests (package R1) found two holes in the credentials Vault
(ADR-033, the task-time store for website logins and external tokens):

1. **Every logged-in user could use it.** All five routes of
   `/api/v1/credentials` (list, get, create, update, delete) only checked
   `require_user`. ADR-033 wrote this down on purpose ("write: every
   logged-in user — the operator via the UI"), at a time when the only
   user was the operator. Since then MC has viewer accounts. A viewer could
   list every stored login, change the password an agent uses, or delete a
   credential. The Settings UI already showed the Vault tab to admins only;
   the API did not follow. `GET /api/v1/secrets` (the masked list of system
   tokens) was open to viewers as well, while its writes were admin-only.
2. **The mask leaked the password.** `encryption.mask()` returned
   `*` × (length − 4) + the last 4 characters, for every secret field,
   including `password`. A 5-character password came back with 4 of its
   5 characters, and every mask told the exact length.

## Entscheidung

1. **The credentials API is admin-only** (`require_role(Role.ADMIN)` on all
   five routes). Agents are not affected: they read credentials through
   their own scope-checked routes (`/api/v1/agent/boards/{id}/credentials`,
   `credentials:read`). This amends ADR-033's "Write-Auth: every logged-in
   user" row; the rest of ADR-033 (two stores, typed credentials, agent
   access) stays.
2. **The masked secret list (`GET /api/v1/secrets`) needs operator or
   higher.** Operators pick an existing key when they bind an agent or add a
   runtime; viewers have no use for it. Writes stay admin-only.
3. **A mask never shows the length.** Every masked value starts with the
   fixed run `••••••••`. Only a value of **20 or more characters** keeps its
   last 4 characters (long random tokens stay recognisable: "which key is
   this?"). **Password-like credential fields** (`password`, `passwd`,
   `passphrase`, `pin`, `secret`, also as a `_…`/`-…` suffix) and free text
   (`content`) never show a tail. The free-text summary in the Vault no
   longer shows a character count.

   **Amended 23.10.2026 (round-4 user tests K5, r1 N1):** the whole-word /
   suffix match let `dbPassword`, `clientSecret`, `secret_key` and
   `totp_seed` show 4 characters, a nested value leaked the end of its
   text form, and `GET /api/v1/secrets` masked without any field rule. Now
   one rule serves both stores (`encryption.mask_credential_field`): the
   field name is normalised (lower case, separators and camelCase folded)
   and matched as a substring. `password`, `passwd`, `pwd`, `passphrase`,
   `pin`, `secret`, `seed`, `private`, `otp`/`totp` and `content` → always
   the fixed run. Only `token`/`key`-like names may keep the 4-character
   tail (20+ characters); every other name — known or not — gets the fixed
   run. A nested value (dict, list) is hidden in full, as one string.

## Alternativen

- **Operator may read credentials, admin writes.** Would keep the
  credential picker in the task form for operators. → Rejected for now:
  the masked list is still a list of every login MC holds, and today no
  operator accounts exist. Can be widened later with its own test.
- **Hide the Vault in the UI only.** Already the case before this ADR —
  and exactly what made the API hole invisible. → Rejected.
- **Keep the old mask for API keys, change only passwords.** → Rejected:
  the length leak applies to every value, and a short "API key" is
  password-sized.

## Später ändern (ADR-093)

Roles are changed per user in Settings → Users; an admin sees and manages
the Vault in Settings → Credentials as before. The mask rule is code
(`services/encryption.py`), not a setting: there is nothing a user should
tune about how much of a secret is shown.

## Konsequenzen

### Positiv
- Viewer accounts can no longer read, change or delete logins and tokens.
- No mask reveals a password character or a secret's length.

### Negativ
- Operators no longer see the credential picker in the task form (the
  query is skipped for non-admins instead of collecting a 403).
- Other write routes still only check `require_user` (round-3 triage:
  role audit as its own pass). This ADR closes the Vault, not the whole
  viewer role.

## Referenzen

- ADR-033 (secrets vs credentials) — amended at the "Write-Auth" row.
- `backend/app/routers/credentials.py`, `backend/app/routers/secrets.py`,
  `backend/app/services/encryption.py`
- Guard tests: `backend/tests/test_credentials_admin_only.py`
- Rules: `R-credentials-admin-only`, `R-mask-no-length-no-password`
  (`docs/produkt/regeln.yaml`)
