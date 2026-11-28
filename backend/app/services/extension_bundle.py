"""Share skills and personas between MC installations: the ``.mcext`` bundle.

A ``.mcext`` file is a zip::

    manifest.json
    skills/<name>/<relative path>      one folder per embedded skill

``manifest.json``::

    {"format": "mcext", "version": 1, "kind": "skill" | "persona",
     "name", "description", "created_with": <MC version>,
     "items": [{"type": "skill", "name", "sha256",
                "files": [{"path", "sha256", "size"}]}],
     "persona": {"name", "description", "emoji", "soul_md", "cli_skills",
                 "model_hint", "not_included": {"plugins": n, "mcp_servers": n}}}

Rules (extensions concept, section 6):

* **Import only requests.** An import validates and stages the content under
  ``~/.mc/extensions/staging/<sha256>/`` and creates PENDING approvals. A
  skill becomes an ``install_skill`` approval with source ``bundle:<sha256>``
  (the install executor copies it after the operator approves); a persona
  becomes one ``import_persona`` approval (the template and its skills are
  created on approval, by the hook at the end of this module).
* **Fixed version.** Every file carries its sha256 and every skill a tree
  sha256 over its files; both are checked on import and again on install.
* **Never overwrite.** An existing skill or template name is refused.
* **Text only.** Only ``.md`` / ``.markdown`` / ``.txt`` files that are valid
  UTF-8, carry no ``#!`` line and no executable bit pass. Everything else
  (scripts, hooks, binaries, unknown types) is refused as ``contains_code``;
  so is a persona that lists plugins or MCP servers. Code is red and refused
  by default in this round.
* **Text that runs is code too.** Claude Code runs parts of a ``SKILL.md``:
  frontmatter ``hooks`` / ``shell``, tool grants (``allowed-tools``) and
  inline shell (``!`cmd` `` and ``!`` fences). The frontmatter of every
  ``SKILL.md`` must use only the keys in ``SKILL_FRONTMATTER_KEYS``; inline
  shell is refused in every text file. Checked on export, import and again
  when the executor loads the staged files.
* **Nothing private leaves.** Export refuses token-shaped secrets and home
  paths, and never writes agent ids, template ids, scopes, env or plugin /
  MCP server lists (only their count).
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import shutil
import stat
import unicodedata
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

logger = logging.getLogger(__name__)

FORMAT = "mcext"
VERSION = 1
MAX_TOTAL_BYTES = 5 * 1024 * 1024   # upload size and uncompressed size
MAX_FILES = 200
TEXT_SUFFIXES = frozenset({".md", ".markdown", ".txt"})
PERSONA_ACTION = "import_persona"
BUNDLE_PREFIX = "bundle:"

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,99}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

# Token shapes that must never leave an installation.
_SECRET_RES = (
    re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{32,}"),
    re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bhf_[A-Za-z0-9]{30,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)
_HOME_PATH_RE = re.compile(r"(/Users/|/home/|[A-Za-z]:\\Users\\)[A-Za-z0-9._-]+")

# SKILL.md frontmatter keys that only describe a skill. Everything else is
# refused: ``hooks`` / ``shell`` run commands, ``allowed-tools`` /
# ``disallowed-tools`` grant tools, and an unknown key could be a capability
# a newer Claude Code adds.
SKILL_FRONTMATTER_KEYS = frozenset({
    "name", "description", "license", "version", "metadata", "argument-hint",
    "when_to_use", "user-invocable", "disable-model-invocation",
})
# Claude Code opens frontmatter with ``---`` + line end (_OPEN) and closes it
# either at a line that is exactly ``---`` (_STRICT) or, in a lenient reader,
# at the first ``---`` anywhere. The lenient block is always a prefix of the
# strict one, so every key it sees is also in the strict block; and when the
# strict reading finds no end, the file is refused. One check covers both.
_FRONTMATTER_OPEN_RE = re.compile(r"\A---\s*\n")
_FRONTMATTER_STRICT_RE = re.compile(r"\A---[ \t]*\r?\n([\s\S]*?)\r?\n---[ \t]*(?:\r?\n|\Z)")
# Inline shell: ``!`cmd` `` anywhere, or a fence opened with ``!``.
_INLINE_SHELL_RE = re.compile(r"!`|^[ \t]*(?:```|~~~)[ \t]*!", re.MULTILINE)


class BundleError(ValueError):
    """A bundle (or an export) is refused. ``reason`` is a stable code."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


# ── hashing ────────────────────────────────────────────────────────────────


def file_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tree_sha256(files: dict[str, bytes]) -> str:
    """One hash for a skill: over the sorted ``path NUL file-sha256`` lines."""
    h = hashlib.sha256()
    for path in sorted(files):
        h.update(f"{path}\0{file_sha256(files[path])}\n".encode())
    return h.hexdigest()


# ── paths ──────────────────────────────────────────────────────────────────


def skills_library() -> Path:
    from app.services.plugin_manager import _custom_skills_dir
    return _custom_skills_dir()


def staging_root() -> Path:
    home = os.environ.get("HOME_HOST") or os.path.expanduser("~")
    return Path(home) / ".mc" / "extensions" / "staging"


def validate_name(name: Any) -> str:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise BundleError("bad_name", f"name {name!r} is not allowed "
                          "(lowercase letters, digits, '-' and '_', max 100)")
    return name


def _safe_relpath(rel: str) -> str:
    """A relative POSIX path that cannot leave its folder."""
    if not rel or "\\" in rel or rel.startswith("/") or "\0" in rel:
        raise BundleError("unsafe_path", f"unsafe path {rel!r}")
    parts = PurePosixPath(rel).parts
    if not parts or any(p in ("", ".", "..") or not _SEGMENT_RE.match(p) for p in parts):
        raise BundleError("unsafe_path", f"unsafe path {rel!r}")
    return "/".join(parts)


# ── content rules ──────────────────────────────────────────────────────────


def check_text_file(rel: str, data: bytes, mode: int = 0) -> None:
    """Refuse anything that is not plain text (``contains_code``)."""
    if PurePosixPath(rel).suffix.lower() not in TEXT_SUFFIXES:
        raise BundleError("contains_code",
                          f"{rel}: only .md/.markdown/.txt files are allowed")
    if mode & 0o111:
        raise BundleError("contains_code", f"{rel}: file is executable")
    if data.startswith(b"#!"):
        raise BundleError("contains_code", f"{rel}: starts with a #! line")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise BundleError("contains_code", f"{rel}: not UTF-8 text")
    if b"\0" in data:
        raise BundleError("contains_code", f"{rel}: binary content")
    if _INLINE_SHELL_RE.search(text):
        raise BundleError("contains_code",
                          f"{rel}: inline shell (!`...` or a ! fence) runs commands")
    if PurePosixPath(rel).name.casefold() == "skill.md":
        check_skill_frontmatter(rel, text)


def check_skill_frontmatter(rel: str, text: str) -> None:
    """Refuse a SKILL.md whose frontmatter carries anything but description
    keys (``contains_code``). Unparseable frontmatter is refused too: what
    cannot be read cannot be shown to be harmless."""
    body = text[1:] if text.startswith("\ufeff") else text
    if not _FRONTMATTER_OPEN_RE.match(body):
        return
    m = _FRONTMATTER_STRICT_RE.match(body)
    if not m:
        raise BundleError("contains_code", f"{rel}: frontmatter is not closed")
    import yaml
    try:
        meta = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        raise BundleError("contains_code", f"{rel}: frontmatter is not valid YAML")
    if meta is None:
        return
    if not isinstance(meta, dict):
        raise BundleError("contains_code", f"{rel}: frontmatter is not a mapping")
    extra = sorted(str(k) for k in meta if k not in SKILL_FRONTMATTER_KEYS)
    if extra:
        raise BundleError(
            "contains_code",
            f"{rel}: frontmatter key(s) {', '.join(extra)} are not shared in "
            "this round (hooks, shell and tool grants run code; only "
            f"{', '.join(sorted(SKILL_FRONTMATTER_KEYS))} are allowed)")


def check_shareable_text(where: str, text: str) -> None:
    """Export guard: no secrets, no home paths."""
    for rx in _SECRET_RES:
        if rx.search(text):
            raise BundleError("contains_secret",
                              f"{where} contains something that looks like a secret")
    if _HOME_PATH_RE.search(text):
        raise BundleError("contains_path", f"{where} contains a home directory path")


# ── export ─────────────────────────────────────────────────────────────────


def _skill_files(root: Path) -> list[tuple[str, Path]]:
    """The files that make up a skill folder: dot segments (``.git``,
    ``.DS_Store``) are skipped. Used for export and for the 'identical skill
    already installed' check, so both see the same skill. A symlink raises."""
    out: list[tuple[str, Path]] = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if any(part.startswith(".") for part in PurePosixPath(rel).parts):
            continue
        if p.is_symlink():
            raise BundleError("unsafe_path", f"{root.name}/{rel}: symlinks are not shared")
        if p.is_file():
            out.append((rel, p))
    return out


def read_skill_dir(name: str) -> dict[str, bytes]:
    """The files of a library skill, checked as an import would check them."""
    validate_name(name)
    root = skills_library() / name
    if root.is_symlink() or not (root / "SKILL.md").is_file():
        raise BundleError("not_found", f"skill {name!r} not found in the library")
    files: dict[str, bytes] = {}
    for rel, p in _skill_files(root):
        data = p.read_bytes()
        _safe_relpath(rel)
        check_text_file(f"{name}/{rel}", data, p.stat().st_mode)
        check_shareable_text(f"{name}/{rel}", data.decode("utf-8"))
        files[rel] = data
    if len(files) > MAX_FILES or sum(map(len, files.values())) > MAX_TOTAL_BYTES:
        raise BundleError("too_large", f"skill {name!r} is larger than a bundle allows")
    return files


def _skill_description(files: dict[str, bytes]) -> str:
    for line in files.get("SKILL.md", b"").decode("utf-8").splitlines()[:30]:
        if line.startswith("description:"):
            return line.split(":", 1)[1].strip().strip("\"'")[:500]
    return ""


def _item(name: str, files: dict[str, bytes]) -> dict[str, Any]:
    return {
        "type": "skill", "name": name, "sha256": tree_sha256(files),
        "files": [{"path": p, "sha256": file_sha256(b), "size": len(b)}
                  for p, b in sorted(files.items())],
    }


def _mc_version() -> str:
    from app.config import settings
    return str(getattr(settings, "app_version", "") or "unknown")


def _zip(manifest: dict[str, Any], skills: dict[str, dict[str, bytes]]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
        for name, files in skills.items():
            for rel, data in sorted(files.items()):
                zf.writestr(f"skills/{name}/{rel}", data)
    data = out.getvalue()
    if len(data) > MAX_TOTAL_BYTES:
        raise BundleError("too_large", "bundle would exceed the size limit")
    return data


def build_skill_bundle(name: str) -> bytes:
    files = read_skill_dir(name)
    manifest = {
        "format": FORMAT, "version": VERSION, "kind": "skill",
        "name": name, "description": _skill_description(files),
        "created_with": _mc_version(), "items": [_item(name, files)],
    }
    return _zip(manifest, {name: files})


def build_persona_bundle(template: Any) -> bytes:
    """A persona from an agent template. Plugins and MCP servers are code and
    are not carried (only counted); scopes stay a local decision."""
    soul = template.soul_md or ""
    check_shareable_text("soul_md", soul)
    for label, value in (("name", template.name), ("role", template.role or ""),
                         ("default_model", template.default_model or "")):
        check_shareable_text(label, value)
    cli_skills = template.cli_skills
    skills: dict[str, dict[str, bytes]] = {}
    for s in cli_skills or []:
        skills[s] = read_skill_dir(s)
    persona = {
        "name": template.name,
        "description": template.role or "",
        "emoji": template.emoji or "",
        "soul_md": soul,
        "cli_skills": list(cli_skills) if cli_skills is not None else None,
        "model_hint": template.default_model,
        "not_included": {"plugins": len(template.cli_plugins or []),
                         "mcp_servers": len(template.mcp_servers or [])},
    }
    manifest = {
        "format": FORMAT, "version": VERSION, "kind": "persona",
        "name": template.name, "description": template.role or "",
        "created_with": _mc_version(),
        "items": [_item(n, f) for n, f in skills.items()],
        "persona": persona,
    }
    return _zip(manifest, skills)


def export_filename(name: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-") or "extension"
    return f"{slug[:80]}.mcext"


# ── import: parse + validate ───────────────────────────────────────────────


@dataclass
class SkillItem:
    name: str
    sha256: str
    files: dict[str, bytes]


@dataclass
class ParsedBundle:
    kind: str
    name: str
    description: str
    bundle_sha256: str
    skills: list[SkillItem] = field(default_factory=list)
    persona: dict[str, Any] | None = None


def _str(v: Any, limit: int, what: str) -> str:
    if v is None:
        return ""
    if not isinstance(v, str) or len(v) > limit:
        raise BundleError("bad_manifest", f"{what} must be text up to {limit} characters")
    return v


def parse_bundle(data: bytes) -> ParsedBundle:
    """Validate a ``.mcext`` completely in memory. Nothing touches disk."""
    if len(data) > MAX_TOTAL_BYTES:
        raise BundleError("too_large", "bundle is larger than 5 MB")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        infos = zf.infolist()
    except (zipfile.BadZipFile, ValueError, OSError):
        raise BundleError("invalid_bundle", "not a .mcext (zip) file")
    with zf:
        if len(infos) > MAX_FILES:
            raise BundleError("too_many_files", f"more than {MAX_FILES} files")
        if sum(i.file_size for i in infos) > MAX_TOTAL_BYTES:
            raise BundleError("too_large", "bundle content is larger than 5 MB")
        entries: dict[str, tuple[bytes, int]] = {}
        folded_seen: dict[str, str] = {}
        for info in infos:
            name = info.filename
            if info.is_dir():
                _safe_relpath(name.rstrip("/"))
                continue
            rel = _safe_relpath(name)
            mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                raise BundleError("unsafe_path", f"{rel}: symlinks are not allowed")
            if rel in entries:
                raise BundleError("invalid_bundle", f"{rel}: listed twice")
            folded = unicodedata.normalize("NFC", rel).casefold()
            if folded in folded_seen:
                # On a case-insensitive disk (macOS) the two would overwrite
                # each other in staging and fail only after approval.
                raise BundleError("unsafe_path", f"{rel}: differs from "
                                  f"{folded_seen[folded]} only in letter case")
            folded_seen[folded] = rel
            try:
                blob = zf.read(info)
            except Exception:
                raise BundleError("invalid_bundle", f"{rel}: cannot be read")
            if len(blob) != info.file_size:
                raise BundleError("invalid_bundle", f"{rel}: size mismatch")
            entries[rel] = (blob, mode)

    if "manifest.json" not in entries:
        raise BundleError("bad_manifest", "manifest.json is missing")
    try:
        manifest = json.loads(entries.pop("manifest.json")[0].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise BundleError("bad_manifest", "manifest.json is not valid JSON")
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise BundleError("bad_manifest", "not an mcext manifest")
    if manifest.get("version") != VERSION:
        raise BundleError("bad_manifest", f"unsupported version {manifest.get('version')!r}")
    kind = manifest.get("kind")
    if kind not in ("skill", "persona"):
        raise BundleError("bad_manifest", f"unknown kind {kind!r}")
    items = manifest.get("items")
    if not isinstance(items, list):
        raise BundleError("bad_manifest", "items must be a list")

    skills: list[SkillItem] = []
    claimed: set[str] = set()
    for it in items:
        if not isinstance(it, dict) or it.get("type") != "skill":
            raise BundleError("contains_code", "only skill items are allowed")
        sname = validate_name(it.get("name"))
        if any(s.name == sname for s in skills):
            raise BundleError("bad_manifest", f"skill {sname!r} listed twice")
        listed = it.get("files")
        if not isinstance(listed, list) or not listed:
            raise BundleError("bad_manifest", f"skill {sname!r} has no files")
        files: dict[str, bytes] = {}
        for f in listed:
            if not isinstance(f, dict):
                raise BundleError("bad_manifest", "file entries must be objects")
            rel = _safe_relpath(_str(f.get("path"), 1000, "file path"))
            key = f"skills/{sname}/{rel}"
            if key not in entries or rel in files:
                raise BundleError("bad_manifest", f"{key} is listed but missing")
            blob, mode = entries[key]
            check_text_file(key, blob, mode)
            if f.get("sha256") != file_sha256(blob):
                raise BundleError("sha_mismatch", f"{key}: sha256 does not match")
            files[rel] = blob
            claimed.add(key)
        if "SKILL.md" not in files:
            raise BundleError("bad_manifest", f"skill {sname!r} has no SKILL.md")
        sha = it.get("sha256")
        if sha != tree_sha256(files):
            raise BundleError("sha_mismatch", f"skill {sname!r}: sha256 does not match")
        skills.append(SkillItem(sname, sha, files))

    unlisted = sorted(set(entries) - claimed)
    if unlisted:
        # A file nobody hashed could be anything: refuse rather than drop it.
        first = unlisted[0]
        blob, mode = entries[first]
        check_text_file(first, blob, mode)
        raise BundleError("bad_manifest", f"{first} is not listed in the manifest")

    persona = None
    if kind == "skill":
        if len(skills) != 1:
            raise BundleError("bad_manifest", "a skill bundle holds exactly one skill")
        name = skills[0].name
    else:
        persona = _parse_persona(manifest.get("persona"), skills)
        name = persona["name"]
    return ParsedBundle(
        kind=kind, name=name,
        description=_str(manifest.get("description"), 2000, "description"),
        bundle_sha256=file_sha256(data), skills=skills, persona=persona,
    )


def _parse_persona(p: Any, skills: list[SkillItem]) -> dict[str, Any]:
    if not isinstance(p, dict):
        raise BundleError("bad_manifest", "persona is missing")
    for code_key in ("cli_plugins", "plugins", "mcp_servers", "mcp", "hooks"):
        if p.get(code_key):
            raise BundleError("contains_code",
                              f"persona lists {code_key}; executable extensions "
                              "are not imported")
    name = _str(p.get("name"), 100, "persona name").strip()
    if not name:
        raise BundleError("bad_name", "persona has no name")
    cli_skills = p.get("cli_skills")
    embedded = {s.name for s in skills}
    if cli_skills is not None:
        if not isinstance(cli_skills, list) or not all(isinstance(s, str) for s in cli_skills):
            raise BundleError("bad_manifest", "cli_skills must be a list of names")
        for s in cli_skills:
            validate_name(s)
        if set(cli_skills) != embedded:
            raise BundleError("bad_manifest",
                              "cli_skills must name exactly the skills in the bundle")
    elif embedded:
        raise BundleError("bad_manifest", "skills in the bundle but cli_skills is empty")
    return {
        "name": name,
        "description": _str(p.get("description"), 500, "description"),
        "emoji": _str(p.get("emoji"), 16, "emoji") or "🤖",
        "soul_md": _str(p.get("soul_md"), 200_000, "soul_md"),
        "cli_skills": list(cli_skills) if cli_skills is not None else None,
        "model_hint": _str(p.get("model_hint"), 200, "model_hint") or None,
    }


# ── staging ────────────────────────────────────────────────────────────────


def stage_skill(item: SkillItem) -> Path:
    """Write a validated skill to ``staging/<sha256>/`` (idempotent)."""
    root = staging_root()
    dest = root / item.sha256
    if dest.is_dir():
        try:
            if load_staged_skill(item.sha256, item.name) == item.files:
                return dest
        except BundleError:
            pass
        shutil.rmtree(dest)
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / f".tmp-{uuid.uuid4().hex}"
    try:
        for rel, data in item.files.items():
            p = tmp / "files" / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        (tmp / "meta.json").write_text(json.dumps({"name": item.name, "sha256": item.sha256}))
        tmp.rename(dest)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return dest


def load_staged_skill(sha: str, name: str) -> dict[str, bytes]:
    """Read a staged skill back and check name, content rules and sha256."""
    if not isinstance(sha, str) or not _SHA_RE.match(sha):
        raise ValueError(f"bundle source needs a sha256, got {sha!r}")
    validate_name(name)
    root = staging_root() / sha
    try:
        meta = json.loads((root / "meta.json").read_text())
    except (OSError, json.JSONDecodeError):
        raise BundleError("not_found", f"no staged bundle {sha[:12]}")
    if meta.get("name") != name:
        raise BundleError("bad_name", f"staged bundle {sha[:12]} is not skill {name!r}")
    base = root / "files"
    files: dict[str, bytes] = {}
    for p in sorted(base.rglob("*")):
        if p.is_symlink():
            raise BundleError("unsafe_path", "staged bundle holds a symlink")
        if p.is_file():
            rel = _safe_relpath(p.relative_to(base).as_posix())
            data = p.read_bytes()
            check_text_file(rel, data, p.stat().st_mode)
            files[rel] = data
    if tree_sha256(files) != sha:
        raise BundleError("sha_mismatch",
                          f"staged skill {name!r} no longer matches sha256 {sha[:12]}")
    return files


def staged_preview(sha: str) -> dict[str, Any]:
    """Read-only view of a staged skill, so the operator can read what an
    ``install_skill`` / ``import_persona`` approval would install. Verified
    exactly as at install (content rules + sha256)."""
    if not isinstance(sha, str) or not _SHA_RE.match(sha):
        raise BundleError("bad_request", "sha256 must be 64 lowercase hex characters")
    try:
        name = json.loads((staging_root() / sha / "meta.json").read_text()).get("name")
    except (OSError, json.JSONDecodeError):
        raise BundleError("not_found", f"no staged bundle {sha[:12]}")
    files = load_staged_skill(sha, name)
    return {"name": name, "sha256": sha,
            "files": [{"path": p, "size": len(b), "text": b.decode("utf-8")}
                      for p, b in sorted(files.items())]}


def library_tree_sha(name: str) -> str | None:
    """Tree sha256 of an installed library skill, None if absent/unreadable."""
    root = skills_library() / name
    if not root.is_dir() or root.is_symlink():
        return None
    try:
        return tree_sha256({rel: p.read_bytes() for rel, p in _skill_files(root)})
    except (BundleError, OSError):
        return None  # a symlink or unreadable file: never "identical"


def install_staged_skill(sha: str, name: str) -> dict[str, Any]:
    """Copy a staged skill into the library (called by the install executor
    for source ``bundle:<sha256>``). Re-verifies sha256; refuses an existing
    name. Returns the executor's install info."""
    from app.services.install_executor import SkillExistsError

    files = load_staged_skill(sha, name)
    lib = skills_library()
    dest = lib / name
    lib.mkdir(parents=True, exist_ok=True)
    try:
        dest.mkdir()  # atomic: an existing (or concurrent) name loses here
    except FileExistsError:
        raise SkillExistsError(
            f"Skill {name!r} already exists in the skills library; "
            "refusing to overwrite it."
        )
    try:
        for rel, data in files.items():
            p = dest / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return {"installed_version": sha[:12], "commit": f"sha256:{sha}", "created_dir": True}


# ── import: approvals ──────────────────────────────────────────────────────


@dataclass
class ImportOutcome:
    approvals: list[dict[str, Any]]
    created: bool


async def _publish_created(approval: Any) -> None:
    try:
        from app.redis_client import RedisKeys, get_redis
        redis = await get_redis()
        await redis.publish(RedisKeys.approvals_events(), json.dumps(
            {"type": "approval.created", "approval_id": str(approval.id),
             "action_type": approval.action_type}))
    except Exception:
        pass  # best effort; the approval is stored


async def create_import_approvals(
    session: Any, parsed: ParsedBundle, *, target_agent_id: uuid.UUID | None,
    user_label: str,
) -> ImportOutcome:
    """Stage the content and create the PENDING approval(s). Installs nothing."""
    from sqlmodel import select

    from app.models.agent import Agent
    from app.models.agent_template import AgentTemplate
    from app.models.approval import Approval

    expires = datetime.now(timezone.utc) + timedelta(days=7)

    if parsed.kind == "skill":
        item = parsed.skills[0]
        if target_agent_id is None:
            raise BundleError("target_required",
                              "a skill import needs target_agent_id (the agent "
                              "that gets the skill)")
        target = await session.get(Agent, target_agent_id)
        if target is None:
            raise BundleError("not_found", f"agent {target_agent_id} not found")
        if (skills_library() / item.name).exists():
            raise BundleError("exists", f"a skill named {item.name!r} already exists; "
                              "it is never overwritten")
        source = f"{BUNDLE_PREFIX}{item.sha256}"
        for row in (await session.exec(select(Approval).where(
                Approval.action_type == "install_skill",
                Approval.status == "pending"))).all():
            pl = row.payload or {}
            if (pl.get("source") == source and pl.get("name") == item.name
                    and pl.get("target_agent_id") == str(target.id)):
                return ImportOutcome([_ref(row, item.name, item.sha256)], created=False)
        stage_skill(item)
        approval = Approval(
            board_id=target.board_id, agent_id=None,
            action_type="install_skill",
            description=(f"{user_label} imported skill {item.name!r} "
                         f"(text only, sha256 {item.sha256[:12]}) for agent "
                         f"{target.name!r}"),
            payload={
                # Same shape as an agent's install request (install_requests.py).
                "name": item.name, "source": source,
                "target_agent_id": str(target.id), "target_agent_name": target.name,
                "requester_agent_id": None, "requester_agent_name": None,
                "reason": f"Imported from a .mcext bundle by {user_label}",
                "proposed_config": None, "requester_task_id": None,
                "ref": None, "commit": None,
                "sha256": item.sha256, "bundle_sha256": parsed.bundle_sha256,
                "files": sorted(item.files),
            },
            autonomy_level="L2", expires_at=expires, status="pending",
        )
        session.add(approval)
        await session.commit()
        await session.refresh(approval)
        await _publish_created(approval)
        return ImportOutcome([_ref(approval, item.name, item.sha256)], created=True)

    # persona
    persona = parsed.persona or {}
    if (await session.exec(select(AgentTemplate).where(
            AgentTemplate.name == persona["name"]))).first():
        raise BundleError("exists", f"a template named {persona['name']!r} already "
                          "exists; it is never overwritten")
    for item in parsed.skills:
        have = library_tree_sha(item.name)
        if (skills_library() / item.name).exists() and have != item.sha256:
            raise BundleError("exists", f"a different skill named {item.name!r} "
                              "already exists; it is never overwritten")
    for row in (await session.exec(select(Approval).where(
            Approval.action_type == PERSONA_ACTION,
            Approval.status == "pending"))).all():
        if (row.payload or {}).get("bundle_sha256") == parsed.bundle_sha256:
            return ImportOutcome([_ref(row, persona["name"], parsed.bundle_sha256)],
                                 created=False)
    for item in parsed.skills:
        stage_skill(item)
    skills_text = ", ".join(f"{s.name}@{s.sha256[:12]}" for s in parsed.skills) or "none"
    approval = Approval(
        board_id=None, agent_id=None, action_type=PERSONA_ACTION,
        description=(f"{user_label} imported persona {persona['name']!r} "
                     f"(skills: {skills_text}; text only, no plugins or MCP "
                     "servers; scopes: developer defaults without credentials)"),
        payload={
            "name": persona["name"], "persona": persona,
            "skills": [{"name": s.name, "sha256": s.sha256} for s in parsed.skills],
            "bundle_sha256": parsed.bundle_sha256,
            "reason": f"Imported from a .mcext bundle by {user_label}",
        },
        autonomy_level="L2", expires_at=expires, status="pending",
    )
    session.add(approval)
    await session.commit()
    await session.refresh(approval)
    await _publish_created(approval)
    return ImportOutcome([_ref(approval, persona["name"], parsed.bundle_sha256)],
                         created=True)


def _ref(approval: Any, name: str, sha: str) -> dict[str, Any]:
    return {"approval_id": str(approval.id), "action_type": approval.action_type,
            "name": name, "sha256": sha, "status": approval.status}


# ── persona approval: create the template ──────────────────────────────────


async def apply_persona_import(session: Any, approval: Any) -> Any:
    """Approved ``import_persona``: install the staged skills (never
    overwrite; an identical existing skill is reused), then create the
    template. Name taken -> nothing is installed."""
    from sqlmodel import select

    from app.models.agent_template import AgentTemplate
    from app.scopes import AgentRole, Scope, get_default_scopes

    payload = approval.payload or {}
    persona = payload.get("persona") or {}
    name = persona.get("name")
    if not name:
        raise BundleError("bad_manifest", "approval has no persona")
    if (await session.exec(select(AgentTemplate).where(
            AgentTemplate.name == name))).first():
        raise BundleError("exists", f"template {name!r} exists; it is never overwritten")

    created: list[str] = []
    try:
        for s in payload.get("skills") or []:
            sname, sha = s["name"], s["sha256"]
            if (skills_library() / sname).exists():
                if library_tree_sha(sname) != sha:
                    raise BundleError("exists", f"a different skill {sname!r} exists")
                continue
            install_staged_skill(sha, sname)
            created.append(sname)
        template = AgentTemplate(
            name=name, emoji=persona.get("emoji") or "🤖",
            role=persona.get("description") or None,
            default_model=persona.get("model_hint"),
            soul_md=persona.get("soul_md") or None,
            cli_skills=persona.get("cli_skills"),
            cli_plugins=[], mcp_servers=[],
            # Scopes are not carried in a bundle. [] would mean "all scopes"
            # at instantiate, so a foreign persona gets the developer set
            # without credential access; the operator widens it if wanted.
            scopes=[s for s in get_default_scopes(AgentRole.DEVELOPER)
                    if s != Scope.CREDENTIALS_READ],
            is_builtin=False,
        )
        session.add(template)
        await session.commit()
        await session.refresh(template)
    except BaseException:
        for sname in created:
            shutil.rmtree(skills_library() / sname, ignore_errors=True)
        raise
    logger.info("persona import %s: template %s created", approval.id, template.id)
    return template


async def on_approval_resolved(session: Any, approval: Any, status: str) -> None:
    """Registered in ``verticals.hooks.approval_resolved_hooks`` (both the UI
    and the quick-resolve path call it for non-core action types)."""
    if approval.action_type != PERSONA_ACTION or status != "approved":
        return
    from app.services.activity import emit_event

    name = ((approval.payload or {}).get("persona") or {}).get("name") or "?"
    try:
        template = await apply_persona_import(session, approval)
    except Exception as e:
        logger.warning("persona import %s failed: %s", approval.id, e)
        try:
            await session.rollback()
        except Exception:
            pass
        reason = getattr(e, "reason", type(e).__name__)
        approval.failure_reason = f"{reason}: {e}"[:2000]
        session.add(approval)
        await session.commit()
        try:
            await emit_event(
                session, event_type="extension.persona_import_failed",
                title=f"Persona import failed: {name} ({reason})", severity="warning",
                detail={"approval_id": str(approval.id), "name": name,
                        "reason": reason, "error": str(e)[:500]},
            )
        except Exception:
            logger.exception("persona import %s: event not stored", approval.id)
        return
    try:
        await emit_event(
            session, event_type="extension.persona_imported",
            title=f"Persona imported: {name}", severity="info",
            detail={"approval_id": str(approval.id), "name": name,
                    "template_id": str(template.id)},
        )
    except Exception:
        logger.exception("persona import %s: event not stored", approval.id)


def register_hooks() -> None:
    from app.verticals import hooks
    if on_approval_resolved not in hooks.approval_resolved_hooks:
        hooks.approval_resolved_hooks.append(on_approval_resolved)
