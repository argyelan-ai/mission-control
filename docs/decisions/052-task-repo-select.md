# ADR-052: Einheitliche Repo-Auswahl in der Task-Maske (Registry statt Toggle)

**Status:** Accepted (2026-07-04) · Ergänzt ADR-050

## Kontext

Nach ADR-050 existierte die Repo-Registry (`/repos`, Regeln, Import), aber die
Task-Maske lebte noch in der alten Welt — drei Doppelspurigkeiten:

1. Ad-hoc: binärer `use_separate_repo`-Toggle erzeugte pro Task ein frisches
   Wegwerf-Repo an der Registry vorbei (Schatten-Repos ohne Regeln).
2. Projekt ohne Repo: nur „Repo anlegen" (neues `mc-…`), kein Verknüpfen
   eines bestehenden Registry-Repos.
3. Kein Hinweis in der Maske, ob Arbeitsregeln aktiv sind.

## Entscheidung

**Eine kanonische Repo-Auswahl aus der Registry, überall:**

- **`tasks.repo_id`** (Migration 0139, nullable FK): explizit gewähltes
  Registry-Repo für Ad-hoc-Aufträge. **Vorrangregel:** Task-Repo (explizit)
  > Projekt-Repo > mc-workspace. Gilt für Workspace-Clone
  (`setup_git_workspace_for_dispatch`, neuer erster Branch; Fehler blockt den
  Task wie beim Projekt-Repo) UND Regel-Injektion
  (`repo_registry.get_repo_rules_for_task` — explizites Repo ohne Regeln
  erbt bewusst NICHT die Projektregeln).
- **`POST /repos/new`**: der einzige Weg, aus der Maske ein neues Repo zu
  erzeugen — erstellt privat unter GITHUB_OWNER, macht den Initial-Commit
  (leeres Repo hätte keinen `main` für den Clone-Pfad) und registriert es.
- **`use_separate_repo` ist deprecated** (API-Kompat bleibt): der Pfad
  registriert sein Wegwerf-Repo jetzt in der Registry und setzt
  `task.repo_id` — keine Schatten-Repos mehr. Die UI bietet den Toggle nicht
  mehr an.
- **`git-info`** liefert `repo_id` + `has_rules` → Regeln-Badge in der Maske.
- Maske: Ad-hoc-Repo-Select (Kein Repo default | Registry | + Neu), Projekt
  ohne Repo zusätzlich „Link existing repo" (Link-Endpoint aus ADR-050).

## Alternativen

- Toggle behalten + Registry-Select daneben: verworfen — genau die
  Doppelspurigkeit, die Mark raus haben will.
- `repo_id` bei Projekt-Tasks verbieten (400): verworfen — Board-Default-
  Projekte setzen `project_id` implizit; klare Vorrangregel ist robuster.

## Konsequenzen

- Jedes Repo, in dem Agenten arbeiten, ist auf `/repos` sichtbar und kann
  Regeln tragen — auch Ad-hoc-Arbeit.
- Der `mc-workspace`-Fallback bleibt für „Kein Repo"-Tasks unverändert.
- Agent-API (`AgentTaskCreate`) kennt `repo_id` noch nicht — bei Bedarf L2.
