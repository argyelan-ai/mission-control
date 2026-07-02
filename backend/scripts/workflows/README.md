# Workflow Scripts

Dieser Ordner ist die erlaubte Heimat fuer `script_ref`-Workflows.

Regeln fuer den MVP:

- nur bewusst freigegebene Scripts hier ablegen
- bevorzugt Python-Scripts
- keine freien Shell-Kommandos aus Workflow-Definitionen
- Eingaben kommen ueber feste `args` oder `WORKFLOW_INPUT`

So bleibt `script_ref` kontrollierbar und trennt Workflow-Scripts sauber von Admin-/Setup-Scripts.
