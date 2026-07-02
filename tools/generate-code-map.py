#!/usr/bin/env python3
"""
Code Map Generator fuer Mission Control (v2).

Scannt Backend UND Frontend und generiert docs/code-map.md mit:
- Alle Models (SQLModel-Klassen + Felder + Foreign Keys)
- Alle Routers (Endpoints + aufgerufene Services)
- Alle Services (Public Functions + Cross-Imports)
- Frontend Components (Imports + API-Calls + Hooks)
- Cross-Stack Mapping (Frontend Component → Backend Router)
- Reverse Dependency Index (welche Dateien haengen von X ab?)
- Change Impact Rules (auto-generierte "Wenn → Dann" Regeln)
- Dependency Graph

Nur stdlib — kein pip install noetig.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

# Projekt-Root relativ zum Script
PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKEND = PROJECT_ROOT / "backend" / "app"
FRONTEND = PROJECT_ROOT / "frontend" / "src"
OUTPUT = PROJECT_ROOT / "docs" / "code-map.md"


# ---------------------------------------------------------------------------
# AST Helpers
# ---------------------------------------------------------------------------

def parse_file(path: Path) -> ast.Module | None:
    """Parse a Python file, return AST or None on error."""
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return None


def get_imports(tree: ast.Module) -> list[str]:
    """Extract all imported names from a module."""
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                imports.append(f"{module}.{alias.name}")
    return imports


def get_decorators(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Get decorator names as strings."""
    result = []
    for dec in node.decorator_list:
        if isinstance(dec, ast.Attribute):
            result.append(f"{ast.dump(dec.value)}.{dec.attr}" if hasattr(dec, 'attr') else dec.attr)
            # Simpler: just grab the attribute
            result[-1] = dec.attr
        elif isinstance(dec, ast.Call):
            if isinstance(dec.func, ast.Attribute):
                result.append(dec.func.attr)
            elif isinstance(dec.func, ast.Name):
                result.append(dec.func.id)
        elif isinstance(dec, ast.Name):
            result.append(dec.id)
    return result


# ---------------------------------------------------------------------------
# Model Scanner
# ---------------------------------------------------------------------------

def scan_models() -> list[dict]:
    """Scan backend/app/models/ for SQLModel classes."""
    models_dir = BACKEND / "models"
    results = []

    for py_file in sorted(models_dir.glob("*.py")):
        if py_file.name == "__init__.py":
            continue

        tree = parse_file(py_file)
        if not tree:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue

            # Check if it inherits from SQLModel (or has table=True)
            base_names = []
            for base in node.bases:
                if isinstance(base, ast.Name):
                    base_names.append(base.id)
                elif isinstance(base, ast.Attribute):
                    base_names.append(base.attr)

            is_sqlmodel = "SQLModel" in base_names

            # Check for table=True in keywords
            is_table = False
            for kw in node.keywords:
                if kw.arg == "table" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                    is_table = True

            if not is_sqlmodel:
                continue

            fields = []
            foreign_keys = []

            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    field_name = item.target.id
                    if field_name.startswith("_"):
                        continue

                    # Get type annotation as string
                    field_type = ast.unparse(item.annotation) if item.annotation else "?"

                    # Check for Foreign Key in Field() call
                    fk = None
                    if item.value and isinstance(item.value, ast.Call):
                        for kw in item.value.keywords:
                            if kw.arg == "foreign_key":
                                if isinstance(kw.value, ast.Constant):
                                    fk = kw.value.value
                                    foreign_keys.append({
                                        "field": field_name,
                                        "references": fk,
                                    })

                    # Check for primary_key
                    is_pk = False
                    if item.value and isinstance(item.value, ast.Call):
                        for kw in item.value.keywords:
                            if kw.arg == "primary_key" and isinstance(kw.value, ast.Constant):
                                is_pk = kw.value.value

                    fields.append({
                        "name": field_name,
                        "type": field_type,
                        "is_pk": is_pk,
                        "fk": fk,
                    })

            results.append({
                "file": py_file.name,
                "class_name": node.name,
                "is_table": is_table,
                "fields": fields,
                "foreign_keys": foreign_keys,
            })

    return results


# ---------------------------------------------------------------------------
# Router Scanner
# ---------------------------------------------------------------------------

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


def scan_routers() -> list[dict]:
    """Scan backend/app/routers/ for API endpoints."""
    routers_dir = BACKEND / "routers"
    results = []

    for py_file in sorted(routers_dir.glob("*.py")):
        if py_file.name == "__init__.py":
            continue

        tree = parse_file(py_file)
        if not tree:
            continue

        source = py_file.read_text(encoding="utf-8")
        imports = get_imports(tree)
        endpoints = []

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            decorators = get_decorators(node)
            method = None
            path = None

            # Check decorators for HTTP methods
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
                    if dec.func.attr in HTTP_METHODS:
                        method = dec.func.attr.upper()
                        if dec.args and isinstance(dec.args[0], ast.Constant):
                            path = dec.args[0].value

            if not method:
                continue

            # Find service calls in the function body
            calls = _extract_calls(node)

            endpoints.append({
                "method": method,
                "path": path or "/",
                "handler": node.name,
                "calls": calls,
                "line": node.lineno,
            })

        # Extract service imports
        service_imports = [i for i in imports if ".services." in i or i.startswith("services.")]

        results.append({
            "file": py_file.name,
            "endpoints": endpoints,
            "service_imports": service_imports,
            "all_imports": imports,
        })

    return results


def _extract_calls(func_node: ast.FunctionDef) -> list[str]:
    """Extract notable function/method calls from a function body."""
    calls = set()
    notable = {
        "emit_event", "broadcast", "make_sse_response",
        "auto_dispatch_task", "enqueue_task", "dequeue_task",
        "encrypt", "decrypt", "safe_decrypt",
        "sync_pipeline_from_task_done", "sync_task_stage_done",
        "_generate_tools_md", "_provision_agent_background",
        "_do_instantiate", "_cleanup_sync_ghosts",
        "_build_dispatch_message", "_find_planning_agent",
        "_send_and_capture_reply", "_extract_and_create_tasks",
        "_find_research_agent",
        "generate_agent_token",
    }
    # Also catch rpc.* and gateway_client.* calls
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in notable:
                calls.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                if isinstance(node.func.value, ast.Name):
                    prefix = node.func.value.id
                    if prefix == "rpc":
                        calls.add(f"rpc.{node.func.attr}()")
                    elif prefix in ("gateway_client", "GatewayClient"):
                        calls.add(f"gateway_client.{node.func.attr}()")
                    elif node.func.attr in notable:
                        calls.add(node.func.attr)
                # BackgroundTasks
                if node.func.attr == "add_task":
                    for arg in node.args:
                        if isinstance(arg, ast.Name):
                            calls.add(f"BG:{arg.id}")
    return sorted(calls)


# ---------------------------------------------------------------------------
# Service Scanner
# ---------------------------------------------------------------------------

def scan_services() -> list[dict]:
    """Scan backend/app/services/ for public functions."""
    services_dir = BACKEND / "services"
    results = []

    for py_file in sorted(services_dir.glob("*.py")):
        if py_file.name == "__init__.py":
            continue

        tree = parse_file(py_file)
        if not tree:
            continue

        imports = get_imports(tree)
        functions = []

        for node in ast.iter_child_nodes(tree):
            # Top-level functions
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not node.name.startswith("_"):
                    functions.append({
                        "name": node.name,
                        "is_async": isinstance(node, ast.AsyncFunctionDef),
                        "line": node.lineno,
                    })

            # Class methods
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if not item.name.startswith("_"):
                            functions.append({
                                "name": f"{node.name}.{item.name}",
                                "is_async": isinstance(item, ast.AsyncFunctionDef),
                                "line": item.lineno,
                            })

        # Cross-service imports
        cross_imports = [
            i for i in imports
            if ".services." in i and py_file.stem not in i
        ]

        results.append({
            "file": py_file.name,
            "functions": functions,
            "cross_imports": cross_imports,
            "all_imports": imports,
        })

    return results


# ---------------------------------------------------------------------------
# Frontend Component Scanner (NEW in v2)
# ---------------------------------------------------------------------------

# Mapping: api group name → backend router file
API_GROUP_TO_ROUTER = {
    "auth": "auth.py",
    "system": "system.py",
    "intelligence": "system.py",
    "boardGroups": "boards.py",
    "boards": "boards.py",
    "projects": "boards.py",
    "planner": "planner.py",
    "content": "content.py",
    "research": "research.py",
    "tasks": "tasks.py",
    "agents": "agents.py",
    "agentTemplates": "agent_templates.py",
    "approvals": "approvals.py",
    "memory": "memory.py",
    "knowledge": "memory.py",
    "chat": "chat.py",
    "activity": "activity.py",
    "gateways": "gateway.py",
    "skills": "skills.py",
    "models": "models.py",
    "openclaw": "gateway.py",
    "tags": "tags.py",
    "secrets": "secrets.py",
    "settings": "settings.py",
    "schedule": "schedule.py",
}

# Regex patterns for frontend scanning
RE_COMPONENT_IMPORT = re.compile(
    r'from\s+["\']@/components/(\S+?)["\']'
)
RE_LIB_IMPORT = re.compile(
    r'from\s+["\']@/lib/(\S+?)["\']'
)
RE_HOOK_IMPORT = re.compile(
    r'from\s+["\']@/hooks/(\S+?)["\']'
)
RE_API_CALL = re.compile(
    r'api\.(\w+)\.(\w+)'
)
RE_USE_SSE = re.compile(r'useSSE\s*\(')
RE_USE_QUERY = re.compile(r'useQuery\s*[<(]')
RE_USE_MUTATION = re.compile(r'useMutation\s*[<(]')
RE_USE_STORE = re.compile(r'useAppStore\s*\(')
RE_SSE_URL = re.compile(r'sseUrls\.(\w+)')


def scan_frontend() -> list[dict]:
    """Scan frontend/src/ for components, their imports and API calls."""
    results = []

    # Scan pages, page-components, shared components, layout, lib
    scan_dirs = [
        ("pages", FRONTEND / "app"),
        ("components/pages", FRONTEND / "components" / "pages"),
        ("components/shared", FRONTEND / "components" / "shared"),
        ("components/layout", FRONTEND / "components" / "layout"),
        ("lib", FRONTEND / "lib"),
        ("hooks", FRONTEND / "hooks"),
    ]

    for category, scan_dir in scan_dirs:
        if not scan_dir.exists():
            continue
        for tsx_file in sorted(scan_dir.glob("*.tsx")) + sorted(scan_dir.glob("*.ts")):
            if tsx_file.name.startswith("_") or ".test." in tsx_file.name:
                continue

            source = tsx_file.read_text(encoding="utf-8")

            # Component imports (@/components/...)
            component_imports = RE_COMPONENT_IMPORT.findall(source)

            # Lib imports (@/lib/...)
            lib_imports = RE_LIB_IMPORT.findall(source)

            # Hook imports (@/hooks/...)
            hook_imports = RE_HOOK_IMPORT.findall(source)

            # API calls (api.group.method)
            api_calls = list(set(RE_API_CALL.findall(source)))

            # Hooks used
            hooks_used = []
            if RE_USE_SSE.search(source):
                hooks_used.append("useSSE")
            if RE_USE_QUERY.search(source):
                hooks_used.append("useQuery")
            if RE_USE_MUTATION.search(source):
                hooks_used.append("useMutation")
            if RE_USE_STORE.search(source):
                hooks_used.append("useAppStore")

            # SSE URL references
            sse_refs = list(set(RE_SSE_URL.findall(source)))

            # Map API calls to backend routers
            backend_routers = set()
            for group, _method in api_calls:
                router = API_GROUP_TO_ROUTER.get(group)
                if router:
                    backend_routers.add(router)

            results.append({
                "file": tsx_file.name,
                "category": category,
                "component_imports": component_imports,
                "lib_imports": lib_imports,
                "hook_imports": hook_imports,
                "api_calls": api_calls,
                "api_groups": sorted(set(g for g, _m in api_calls)),
                "hooks_used": hooks_used,
                "sse_refs": sse_refs,
                "backend_routers": sorted(backend_routers),
            })

    return results


# ---------------------------------------------------------------------------
# Dependency Graph (enhanced in v2)
# ---------------------------------------------------------------------------

def build_dependency_graph(routers: list[dict], services: list[dict]) -> dict[str, set[str]]:
    """Build a dependency graph: file -> set of files it imports from."""
    graph: dict[str, set[str]] = {}

    # Router → Service dependencies
    for r in routers:
        deps = set()
        for imp in r["all_imports"]:
            for s in services:
                sname = s["file"].replace(".py", "")
                if sname in imp and sname != r["file"].replace(".py", ""):
                    deps.add(s["file"])
        if deps:
            graph[r["file"]] = deps

    # Service → Service dependencies
    for s in services:
        deps = set()
        for imp in s["cross_imports"]:
            for other in services:
                oname = other["file"].replace(".py", "")
                if oname in imp and oname != s["file"].replace(".py", ""):
                    deps.add(other["file"])
        if deps:
            key = f"services/{s['file']}"
            graph[key] = {f"services/{d}" for d in deps}

    return graph


def build_reverse_index(
    graph: dict[str, set[str]],
    frontend: list[dict],
) -> dict[str, list[str]]:
    """Build reverse dependency index: file -> list of files that depend on it.

    Combines backend dependency graph with frontend->backend connections.
    """
    reverse: dict[str, set[str]] = {}

    # Backend reverse deps
    for source, deps in graph.items():
        for dep in deps:
            reverse.setdefault(dep, set()).add(source)

    # Frontend → Backend router deps
    for comp in frontend:
        if not comp["backend_routers"]:
            continue
        fe_label = f"frontend:{comp['category']}/{comp['file']}"
        for router in comp["backend_routers"]:
            reverse.setdefault(router, set()).add(fe_label)

    # Frontend → Frontend lib/component deps
    for comp in frontend:
        fe_label = f"frontend:{comp['category']}/{comp['file']}"
        for imp in comp["lib_imports"]:
            lib_file = imp.split("/")[-1]
            if not lib_file.endswith(".ts") and not lib_file.endswith(".tsx"):
                lib_file += ".ts"
            lib_key = f"frontend:lib/{lib_file}"
            reverse.setdefault(lib_key, set()).add(fe_label)
        for imp in comp["component_imports"]:
            parts = imp.split("/")
            comp_file = parts[-1]
            if not comp_file.endswith(".tsx"):
                comp_file += ".tsx"
            if len(parts) >= 2:
                comp_key = f"frontend:components/{parts[-2]}/{comp_file}"
            else:
                comp_key = f"frontend:components/{comp_file}"
            reverse.setdefault(comp_key, set()).add(fe_label)

    # Sort by count (most depended-on first)
    return {
        k: sorted(v)
        for k, v in sorted(reverse.items(), key=lambda x: -len(x[1]))
    }


def generate_impact_rules(
    reverse_index: dict[str, list[str]],
    graph: dict[str, set[str]],
) -> list[dict]:
    """Generate change impact rules for high-risk files.

    Returns list of {file, dependents_count, category, rule}.
    """
    rules = []

    for file_key, dependents in reverse_index.items():
        count = len(dependents)
        if count < 3:
            continue

        # Categorize
        if file_key.startswith("frontend:"):
            category = "frontend"
        elif file_key.startswith("services/"):
            category = "service"
        else:
            category = "router"

        # Build human-readable rule
        # Group dependents by type
        backend_deps = [d for d in dependents if not d.startswith("frontend:")]
        frontend_deps = [d for d in dependents if d.startswith("frontend:")]

        parts = []
        if backend_deps:
            short = [d.replace("services/", "").replace(".py", "") for d in backend_deps[:6]]
            if len(backend_deps) > 6:
                short.append(f"+{len(backend_deps) - 6} weitere")
            parts.append(f"Backend: {', '.join(short)}")
        if frontend_deps:
            short = [
                d.replace("frontend:", "").replace(".tsx", "").replace(".ts", "")
                for d in frontend_deps[:4]
            ]
            if len(frontend_deps) > 4:
                short.append(f"+{len(frontend_deps) - 4} weitere")
            parts.append(f"Frontend: {', '.join(short)}")

        rule = " | ".join(parts)

        rules.append({
            "file": file_key,
            "dependents_count": count,
            "category": category,
            "rule": rule,
            "backend_deps": backend_deps,
            "frontend_deps": frontend_deps,
        })

    # Sort by dependents count desc
    rules.sort(key=lambda r: -r["dependents_count"])
    return rules


# ---------------------------------------------------------------------------
# Markdown Generator (enhanced in v2)
# ---------------------------------------------------------------------------

def generate_markdown(
    models: list[dict],
    routers: list[dict],
    services: list[dict],
    graph: dict[str, set[str]],
    frontend: list[dict],
    reverse_index: dict[str, list[str]],
    impact_rules: list[dict],
) -> str:
    """Generate the full code-map.md content."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Code Map — Mission Control",
        f"*Auto-generated by `tools/generate-code-map.py` (v2) — {now}*",
        "",
    ]

    # --- Stats ---
    table_models = [m for m in models if m["is_table"]]
    non_table_models = [m for m in models if not m["is_table"]]
    total_endpoints = sum(len(r["endpoints"]) for r in routers)
    total_functions = sum(len(s["functions"]) for s in services)
    fe_components = [f for f in frontend if f["category"].startswith("components/")]
    fe_pages = [f for f in frontend if f["category"] == "pages"]

    lines.append(
        f"**{len(table_models)} Tabellen** | "
        f"**{len(non_table_models)} Schema-Models** | "
        f"**{len(routers)} Router** ({total_endpoints} Endpoints) | "
        f"**{len(services)} Services** ({total_functions} Functions) | "
        f"**{len(fe_pages)} Pages** | "
        f"**{len(fe_components)} Components**"
    )
    lines.append("")

    # =======================================================================
    # CHANGE IMPACT RULES (new in v2 — most important section, first)
    # =======================================================================
    lines.append("---")
    lines.append("## Change Impact Rules")
    lines.append("")
    lines.append("*Auto-generiert aus dem Dependency Graph. Dateien mit den meisten Abhaengigen zuerst.*")
    lines.append("")
    lines.append("| Datei | Abhaengige | Bei Aenderung pruefen |")
    lines.append("|-------|------------|----------------------|")

    for rule in impact_rules[:20]:
        file_short = rule["file"]
        lines.append(
            f"| **{file_short}** | {rule['dependents_count']} | {rule['rule']} |"
        )

    lines.append("")

    # =======================================================================
    # REVERSE DEPENDENCY INDEX (new in v2)
    # =======================================================================
    lines.append("---")
    lines.append("## Reverse Dependency Index")
    lines.append("")
    lines.append("*Wenn du eine Datei aenderst — diese Dateien sind betroffen:*")
    lines.append("")

    shown = 0
    for file_key, dependents in reverse_index.items():
        if len(dependents) < 2:
            continue
        if shown >= 25:
            break
        shown += 1

        deps_short = []
        for d in dependents[:8]:
            deps_short.append(d)
        suffix = f" +{len(dependents) - 8} weitere" if len(dependents) > 8 else ""

        lines.append(f"**{file_key}** ({len(dependents)} Abhaengige)")
        for d in deps_short:
            lines.append(f"  - {d}")
        if suffix:
            lines.append(f"  - {suffix.strip()}")
        lines.append("")

    # =======================================================================
    # FRONTEND (new in v2)
    # =======================================================================
    lines.append("---")
    fe_with_api = [f for f in frontend if f["api_calls"]]
    lines.append(f"## Frontend ({len(fe_pages)} Pages, {len(fe_components)} Components, {len(fe_with_api)} mit API-Calls)")
    lines.append("")

    # Pages with their API dependencies
    lines.append("### Pages → API Abhaengigkeiten")
    lines.append("")
    lines.append("| Page | API Gruppen | Backend Router | Hooks |")
    lines.append("|------|-------------|---------------|-------|")

    for comp in sorted(frontend, key=lambda c: (c["category"], c["file"])):
        if comp["category"] not in ("pages", "components/pages"):
            continue
        if not comp["api_calls"] and not comp["hooks_used"]:
            continue

        api_groups = ", ".join(comp["api_groups"]) if comp["api_groups"] else "—"
        routers_str = ", ".join(comp["backend_routers"]) if comp["backend_routers"] else "—"
        hooks = ", ".join(comp["hooks_used"]) if comp["hooks_used"] else "—"

        lines.append(f"| {comp['file']} | {api_groups} | {routers_str} | {hooks} |")

    lines.append("")

    # Shared components with API calls
    shared_with_api = [
        f for f in frontend
        if f["category"].startswith("components/") and f["category"] != "components/pages" and f["api_calls"]
    ]
    if shared_with_api:
        lines.append("### Shared Components mit API-Calls")
        lines.append("")
        lines.append("| Component | API Gruppen | Backend Router |")
        lines.append("|-----------|-------------|---------------|")
        for comp in shared_with_api:
            api_groups = ", ".join(comp["api_groups"])
            routers_str = ", ".join(comp["backend_routers"])
            lines.append(f"| {comp['file']} | {api_groups} | {routers_str} |")
        lines.append("")

    # Cross-Stack Mapping
    lines.append("### Cross-Stack Mapping (Frontend → Backend)")
    lines.append("")
    lines.append("*Welche Frontend-Dateien nutzen welchen Backend-Router:*")
    lines.append("")

    # Group by backend router
    router_to_frontend: dict[str, list[str]] = {}
    for comp in frontend:
        for router in comp["backend_routers"]:
            router_to_frontend.setdefault(router, []).append(
                f"{comp['category']}/{comp['file']}"
            )

    lines.append("| Backend Router | Frontend-Dateien |")
    lines.append("|---------------|-----------------|")
    for router, fe_files in sorted(router_to_frontend.items()):
        fe_short = ", ".join(sorted(set(fe_files))[:5])
        if len(set(fe_files)) > 5:
            fe_short += f" +{len(set(fe_files)) - 5}"
        lines.append(f"| {router} | {fe_short} |")

    lines.append("")

    # =======================================================================
    # MODELS (existing)
    # =======================================================================
    lines.append("---")
    lines.append(f"## Models ({len(table_models)} Tabellen, {len(non_table_models)} Schemas)")
    lines.append("")

    # Table models
    lines.append("### Tabellen (table=True)")
    lines.append("")
    lines.append("| Tabelle | Datei | Felder | Foreign Keys |")
    lines.append("|---------|-------|--------|-------------|")

    for m in table_models:
        field_names = [f["name"] for f in m["fields"] if not f["is_pk"]]
        fk_list = [f"`{fk['field']}` → {fk['references']}" for fk in m["foreign_keys"]]
        lines.append(
            f"| **{m['class_name']}** | {m['file']} | "
            f"{', '.join(field_names[:8])}{'...' if len(field_names) > 8 else ''} | "
            f"{', '.join(fk_list) if fk_list else '—'} |"
        )

    lines.append("")

    # Schema models
    if non_table_models:
        lines.append("### Schemas (table=False, Pydantic-Validation)")
        lines.append("")
        lines.append("| Schema | Datei | Felder |")
        lines.append("|--------|-------|--------|")
        for m in non_table_models:
            field_names = [f["name"] for f in m["fields"]]
            lines.append(
                f"| **{m['class_name']}** | {m['file']} | "
                f"{', '.join(field_names[:8])}{'...' if len(field_names) > 8 else ''} |"
            )
        lines.append("")

    # =======================================================================
    # ROUTERS (existing)
    # =======================================================================
    lines.append("---")
    lines.append(f"## Routers ({len(routers)} Router, {total_endpoints} Endpoints)")
    lines.append("")

    for r in routers:
        if not r["endpoints"]:
            continue
        lines.append(f"### {r['file']}")
        lines.append("")
        lines.append("| Method | Path | Handler | Calls |")
        lines.append("|--------|------|---------|-------|")

        for ep in r["endpoints"]:
            calls_str = ", ".join(ep["calls"]) if ep["calls"] else "—"
            lines.append(
                f"| {ep['method']} | `{ep['path']}` | {ep['handler']}() | {calls_str} |"
            )
        lines.append("")

    # =======================================================================
    # SERVICES (existing)
    # =======================================================================
    lines.append("---")
    lines.append(f"## Services ({len(services)} Services, {total_functions} Functions)")
    lines.append("")

    for s in services:
        if not s["functions"]:
            continue
        lines.append(f"### {s['file']}")

        if s["cross_imports"]:
            short_imports = [i.split(".")[-1] for i in s["cross_imports"]]
            lines.append(f"*Imports:* {', '.join(set(short_imports))}")

        lines.append("")
        lines.append("| Function | Async | Line |")
        lines.append("|----------|-------|------|")

        for f in s["functions"]:
            async_mark = "async" if f["is_async"] else ""
            lines.append(f"| {f['name']}() | {async_mark} | L{f['line']} |")
        lines.append("")

    # =======================================================================
    # DEPENDENCY GRAPH (existing)
    # =======================================================================
    lines.append("---")
    lines.append("## Dependency Graph")
    lines.append("")
    lines.append("Router → Service Abhaengigkeiten:")
    lines.append("```")

    for source, deps in sorted(graph.items()):
        if not source.startswith("services/"):
            dep_str = ", ".join(sorted(deps))
            lines.append(f"  {source} → {dep_str}")

    lines.append("```")
    lines.append("")
    lines.append("Service → Service Abhaengigkeiten:")
    lines.append("```")

    for source, deps in sorted(graph.items()):
        if source.startswith("services/"):
            dep_str = ", ".join(sorted(deps))
            short = source.replace("services/", "")
            short_deps = dep_str.replace("services/", "")
            lines.append(f"  {short} → {short_deps}")

    lines.append("```")
    lines.append("")

    # =======================================================================
    # QUICK REFERENCE (existing, enhanced)
    # =======================================================================
    lines.append("---")
    lines.append("## Quick Reference")
    lines.append("")
    lines.append("| Wenn du ... aendern willst | Lies zuerst |")
    lines.append("|---------------------------|-------------|")
    lines.append("| Task-Status-Logik | `routers/tasks.py` (VALID_TRANSITIONS), `docs/flows/task-lifecycle.md` |")
    lines.append("| Dispatch-Reihenfolge | `services/dispatch.py`, `services/task_runner.py`, `docs/flows/dispatch-system.md` |")
    lines.append("| Agent Provisioning | `routers/agents.py` (_provision_agent_background), `docs/flows/agent-provisioning.md` |")
    lines.append("| Gateway-Kommunikation | `services/openclaw_rpc.py`, `docs/flows/gateway-rpc.md` |")
    lines.append("| Watchdog-Verhalten | `services/watchdog.py`, `docs/flows/watchdog-system.md` |")
    lines.append("| Planner/Phasen | `routers/planner.py`, `docs/flows/planner-flow.md` |")
    lines.append("| Content-Pipeline | `routers/content.py`, `services/pipeline_sync.py` |")
    lines.append("| SSE-Events | `services/sse.py`, `services/activity.py` |")
    lines.append("| Auth/Tokens | `routers/auth.py`, `backend/app/auth.py` |")
    lines.append("| Knowledge Base | `routers/memory.py` (Knowledge-Endpoints), `models/memory.py` |")
    lines.append("| Secrets/Encryption | `routers/secrets.py`, `services/encryption.py` |")
    lines.append("| Board Rules | `routers/tasks.py` (_enforce_board_rules), `models/board.py` |")
    lines.append("| Frontend API Client | `lib/api.ts` — alle Pages die betroffene Endpoints nutzen pruefen |")
    lines.append("| Frontend Types | `lib/types.ts` — alle Components die betroffene Interfaces nutzen pruefen |")
    lines.append("| Frontend State | `lib/store.ts` — Sidebar, Board-Context, Notifications |")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("Scanning models...")
    models = scan_models()
    table_count = len([m for m in models if m["is_table"]])
    schema_count = len([m for m in models if not m["is_table"]])
    print(f"  Found {table_count} tables, {schema_count} schemas")

    print("Scanning routers...")
    routers = scan_routers()
    ep_count = sum(len(r["endpoints"]) for r in routers)
    print(f"  Found {len(routers)} routers, {ep_count} endpoints")

    print("Scanning services...")
    services = scan_services()
    fn_count = sum(len(s["functions"]) for s in services)
    print(f"  Found {len(services)} services, {fn_count} functions")

    print("Scanning frontend...")
    frontend = scan_frontend()
    fe_pages = [f for f in frontend if f["category"] in ("pages", "components/pages")]
    fe_comps = [f for f in frontend if f["category"].startswith("components/")]
    print(f"  Found {len(fe_pages)} pages, {len(fe_comps)} components, {len(frontend)} total files")

    print("Building dependency graph...")
    graph = build_dependency_graph(routers, services)

    print("Building reverse dependency index...")
    reverse_index = build_reverse_index(graph, frontend)
    print(f"  {len(reverse_index)} entries, top: {list(reverse_index.keys())[:3]}")

    print("Generating change impact rules...")
    impact_rules = generate_impact_rules(reverse_index, graph)
    high_risk = [r for r in impact_rules if r["dependents_count"] >= 5]
    print(f"  {len(impact_rules)} rules, {len(high_risk)} high-risk (>=5 dependents)")

    print("Generating markdown...")
    md = generate_markdown(models, routers, services, graph, frontend, reverse_index, impact_rules)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(md, encoding="utf-8")
    print(f"Written to {OUTPUT}")
    print(f"  {len(md)} characters, {md.count(chr(10))} lines")


if __name__ == "__main__":
    main()
