"""Bench W1 API contract — the ONE shape every W1 package builds against.

Frozen by the W1 contract commit (24.10.2026). Package A fills the routes,
packages B/C/D consume the mirrored TypeScript types in
``frontend-v2/src/verticals/bench_studio/w1/types.ts``. A change here needs
the lead's OK and the TS mirror + fixtures in the same commit (CONTRACT.md).

Rules baked into the shapes (concept 30 / operator decisions 22./24.10.):
- Names are DERIVED: ``display_name`` is always the served model id verbatim
  (gate R2); an ``alias`` may only appear next to it.
- Money is never a bare number: ``Money.cost_kind`` says what a value means
  (local / api_price / subscription / unknown); local runs carry a small
  hypothetical API price only for comparison (D3).
- Class A (model pure) and class B (agent builds) never share a table:
  ``BenchTestDetail.blocks`` holds one block per class, A first (§5.1).
- A missing headline number is ``Stat.measured = False`` + ``reason``
  (K15), never a guess and never "—".
- Effort on the agent path is display-only (D5): harness options with
  ``effort_settable = False``.
- UI words are NOT in these payloads: the backend sends codes + params, the
  frontend words them via i18n (EN/DE). Exception: the X-Kit texts, which
  are English public artefacts by decision (24.10.).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# ── Enumerations ──────────────────────────────────────────────────────────

Harness = Literal["api", "omp", "hermes", "grok-build", "claude-code"]
RunClass = Literal["A", "B", "C"]
Effort = Literal["default", "off", "on", "low", "medium", "high", "max"]
CostKind = Literal["local", "api_price", "subscription", "unknown"]
Locality = Literal["local", "cloud"]
PresetId = Literal["standard", "quick", "effort_ladder", "rerun"]
Trigger = Literal["runtimes", "quick", "template", "rerun", "auto_switch", "list"]
# Human states (concept §3): Läuft · Wartet (Grund) · Fertig ·
# Fertig – Zahlen fehlen (Grund) · Abgebrochen (Grund)
BenchTestState = Literal["waiting", "running", "done", "done_missing", "aborted"]
# List sections (P7): Läuft (running + waiting) · Braucht Blick · Fertig
Section = Literal["running", "needs_look", "done"]
GateStatus = Literal["green", "amber", "red", "pending"]
RunState = Literal["waiting", "running", "done", "failed"]
RunPhase = Literal["queued", "generating", "recording", "composing", "done"]
DeviceState = Literal["free", "busy", "asleep", "offline"]
ProbeStatus = Literal["ok", "only_on_off", "no_effect", "failed", "unchecked"]
WaitCode = Literal[
    "model_fresh",      # serving_since < 15 min (cache-dropper rule) — until = start time
    "box_busy",         # engine in use / fleet usage in the last 2 min
    "model_loading",    # auto test: recipe switch still loading
    "after_class_a",    # class B waits for the last class A run (§5.1)
    "box_asleep",       # a wake-on-LAN box is asleep
]
BlockerCode = Literal[
    "box_busy", "model_fresh", "box_asleep", "engine_down", "agent_busy",
    "protocol_mismatch", "no_api_key", "no_model",
]
AbortCode = Literal[
    "stopped_by_operator", "task_deleted", "superseded_by_switch",
    "switch_failed", "all_runs_failed", "backend_restart",
]
GateRule = Literal["R1", "R2", "R3", "R4", "R5", "R6", "amber"]
# Library lane: "three-local" = the recorder served MC's pinned three.js for
# the page's CDN request; "none" = no library from MC (25.10.2026).
LibTrack = Literal["three-local", "none"]
KitFileName = Literal["video.mp4", "card.png", "kit.zip"]


class Reason(BaseModel):
    """A machine code + parameters — the UI words it (i18n benchShared)."""
    code: str
    params: dict[str, Any] = Field(default_factory=dict)


# ── Shared building blocks ───────────────────────────────────────────────


class DurationRange(BaseModel):
    """Honest estimate (critic 32 S4): a range from history, never a point
    guess. ``basis``: history = past runs of this runtime/harness,
    default = no history yet (preset default), probe = reasoning-token rate."""
    min_s: int | None = None
    max_s: int | None = None
    basis: Literal["history", "default", "probe"] = "default"


class Money(BaseModel):
    """What a test costs, per D3/D4.

    - local: ``usd`` None; ``hypothetical_usd`` = API list price for
      comparison only (shown small, never on a button).
    - subscription: "über dein Ollama-Abo"; ``hypothetical_usd`` small.
    - api_price: real money; ``needs_confirmation`` True; over $1
      ``second_confirmation`` True (D4).
    - unknown: ``reason`` says why ("*" price fallback is unknown, never $0).
    """
    cost_kind: CostKind
    usd: float | None = None
    hypothetical_usd: float | None = None
    price_source: str | None = None
    price_date: str | None = None  # ISO date of the price table row
    needs_confirmation: bool = False
    second_confirmation: bool = False
    reason: Reason | None = None


class Device(BaseModel):
    """Public device facts — display names only, never IPs/hostnames (§8)."""
    name: str                       # "DGX Spark ×2", "RTX 5090"
    nodes: int | None = None
    gpu_mode: str | None = None     # eco | eco+ | normal | boost
    state: DeviceState = "free"


class Blocker(BaseModel):
    """Why a contestant cannot start now. ``blocking`` False = the test
    still starts and WAITS (``until`` when known); True = Start disabled."""
    code: BlockerCode
    blocking: bool
    until: datetime | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class EffortOption(BaseModel):
    level: Effort
    # "hoch (Standard)": the server default merged into the level it maps to.
    is_default: bool = False
    merged_level: Effort | None = None


class EffortProbeView(BaseModel):
    """The effort chips as the probe really found them (critic 32 S3)."""
    status: ProbeStatus
    options: list[EffortOption] = Field(default_factory=list)
    # First probe after serving_since — the stamp "geprüft am 21.10.".
    probed_at: datetime | None = None
    default_observed: Literal["on", "off", "unknown"] = "unknown"


class HarnessOption(BaseModel):
    harness: Harness
    run_class: RunClass
    available: bool = True
    reason: Reason | None = None
    # D5: only the API path can set effort in W1.
    effort_settable: bool = False
    # Agent path: what the session will run on, observed after the run.
    effort_note: Reason | None = None
    estimate: DurationRange = Field(default_factory=DurationRange)
    n_runs: int = 1


class LastTest(BaseModel):
    id: uuid.UUID
    title: str
    created_at: datetime


class Contestant(BaseModel):
    """One testable model (runtime) — local rows deduped per engine
    (``pairs._dedupe_local``), cloud rows one per runtime."""
    key: str                                   # "<runtime_id>"
    runtime_id: uuid.UUID
    served_model_id: str | None = None
    display_name: str                          # == served_model_id (R2)
    alias: str | None = None
    locality: Locality
    provider: str | None = None                # "Ollama Cloud", "xAI"
    device: Device | None = None               # local only
    engine: str | None = None                  # None = unknown (printed as unknown)
    quant: str | None = None
    recipe_slug: str | None = None
    spec_decoding: str | None = None           # None = unknown
    serving_since: datetime | None = None
    harnesses: list[HarnessOption] = Field(default_factory=list)
    effort: EffortProbeView
    money: Money
    blockers: list[Blocker] = Field(default_factory=list)
    # N7: "gleicher Modellname · Cloud-Version unbekannt"
    cloud_build_unknown: bool = False
    last_test: LastTest | None = None


class ContestantList(BaseModel):
    local: list[Contestant] = Field(default_factory=list)
    cloud: list[Contestant] = Field(default_factory=list)


class PromptRef(BaseModel):
    template_id: uuid.UUID | None = None
    title: str
    sha256: str


class PresetView(BaseModel):
    id: PresetId
    n_runs: int                    # class A runs per contestant
    multi_effort: bool = False     # effort ladder: explicit multi-select
    prompt: PromptRef


class ContestantPick(BaseModel):
    """What the sheet sends per contestant row."""
    runtime_id: uuid.UUID
    harness: Harness = "api"
    # One level normally; 2-3 only for the effort ladder (multi-select).
    efforts: list[Effort] = Field(default_factory=lambda: ["default"], min_length=1, max_length=3)


class BenchPlan(BaseModel):
    """GET /bench/plan — everything pre-filled; the sheet only shows it."""
    preset: PresetView
    presets: list[PresetView]
    contestants: list[Contestant]    # [0] = the subject; more = opponents picked
    picks: list[ContestantPick]      # default selection = 0 typed fields
    estimate: DurationRange
    money: Money                     # total of all picks
    blockers: list[Blocker] = Field(default_factory=list)
    # Mixed A + B request: runs one after the other, two result blocks.
    sequential: bool = False
    video_run_index: int = 1
    max_video_contestants: int = 2


class BenchTestCreate(BaseModel):
    """POST /bench/tests. A cost run needs ``cost_confirmed_usd`` ≥ the
    quoted amount (409 ``bench_cost_confirmation`` step 1 otherwise) and,
    over $1, ``confirm_over_cap`` (step 2)."""
    preset: PresetId = "standard"
    prompt_template_id: uuid.UUID | None = None
    picks: list[ContestantPick] = Field(min_length=1, max_length=2)
    trigger: Trigger = "list"
    rerun_of: uuid.UUID | None = None
    budget: Literal["same", "larger"] = "same"
    cost_confirmed_usd: float | None = None
    confirm_over_cap: bool = False
    # D4 fail-closed: a cloud API with no known price asks "Kosten
    # unbekannt — OK?" first (409 CostConfirmation with price_unknown).
    confirm_unknown_cost: bool = False


class CostConfirmation(BaseModel):
    """409 detail body when money must be confirmed first. ``usd`` None +
    ``price_unknown`` = a cloud API without a price row: the amount cannot
    be quoted, the operator confirms "unknown" explicitly."""
    code: Literal["bench_cost_confirmation"] = "bench_cost_confirmation"
    step: Literal[1, 2]
    usd: float | None = None
    cap_usd: float = 1.0
    price_unknown: bool = False


# ── Results ──────────────────────────────────────────────────────────────


class Stat(BaseModel):
    """Median + range over the n runs of one contestant. ``measured`` False
    = headline number missing; ``reason`` says why (K15)."""
    measured: bool = True
    median: float | None = None
    min: float | None = None
    max: float | None = None
    n: int = 0
    reason: Reason | None = None


class EffortRecord(BaseModel):
    requested: Effort = "default"
    # The level the server default maps to, as the probe found it at start
    # ("hoch (Standard)" in the sheet, on the result page and on the card).
    default_level: Effort | None = None
    sent: dict[str, Any] | None = None        # {key, value} raw; None = nothing sent
    observed: dict[str, Any] | None = None    # {thinking, reasoning_tokens} | {source, level}


class RunView(BaseModel):
    """One repetition (live and finished)."""
    entry_id: uuid.UUID
    repeat_idx: int
    attempt_no: int = 1
    state: RunState
    phase: RunPhase = "queued"
    elapsed_ms: int | None = None
    tokens_out: int | None = None             # so far while running
    decode_tok_s: float | None = None         # class A only
    ttft_ms: int | None = None
    finish_reason: str | None = None
    truncated: bool = False                   # finish_reason == "length"
    wait: Reason | None = None                # WaitCode while waiting
    error: Reason | None = None
    is_video_run: bool = False


class Summary(BaseModel):
    """Per contestant. Class B has no tempo (decode/ttft omitted = None)."""
    time: Stat                                # A: gen_ms · B: active_ms
    time_kind: Literal["gen_ms", "active_ms"] = "gen_ms"
    wait_ms: int | None = None                # B only
    tokens_out: Stat
    tokens_reasoning: Stat | None = None
    tokens_cache_read: int | None = None
    decode_tok_s: Stat | None = None
    ttft_ms: Stat | None = None
    cost: Money
    page_check: Literal["loads", "blank", "unknown"] | None = None
    tie_with: list[str] = Field(default_factory=list)  # contestant keys whose ranges overlap


class Receipt(BaseModel):
    """Concept §7.2 — every field optional; ``unknown`` names each field
    that could not be known and why ("engine": "hand_made_runtime")."""
    model_config = {"extra": "allow"}

    protocol_version: Literal["v0", "v1"] = "v1"
    run_class: RunClass = "A"
    served_model_id: str | None = None
    display_name: str | None = None
    alias: str | None = None
    locality: Locality | None = None
    device: str | None = None
    nodes: int | None = None
    provider: str | None = None
    engine: str | None = None
    engine_version: str | None = None
    recipe_slug: str | None = None
    quant: str | None = None
    gpu_mode: str | None = None
    spec_decoding: str | None = None
    harness: Harness | None = None
    harness_version: str | None = None
    effort_requested: Effort | None = None
    effort_sent: dict[str, Any] | None = None
    effort_observed: dict[str, Any] | None = None
    thinking_key: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    # How max_tokens was set (25.10.2026): cap · context (local, the whole
    # window) · engine (local, none sent). None on older receipts.
    max_tokens_rule: Literal["cap", "context", "engine"] | None = None
    system_prompt_sha: str | None = None
    prompt_sha256: str | None = None
    brief_sha: str | None = None              # class B
    tools_allowed: bool | None = None         # class B
    time_limit_min: int | None = None         # class B; class A since 25.10.2026
    n_runs: int | None = None
    video_run_index: int | None = None
    attempts: int | None = None
    failures: int | None = None
    network_blocked: bool | None = None
    network_attempts: int | None = None
    network_blocked_scripts: int | None = None  # of those, scripts/modules (gate R6)
    libraries_inlined: list[str] = Field(default_factory=list)
    # Lane lib:three-local (only on recordings that reported it).
    lib_track: LibTrack | None = None
    libraries_provided: list[str] = Field(default_factory=list)  # ["three.js r170"]
    three_requested: list[str] | None = None  # versions the page asked for ["r160"]
    three_version_mismatch: bool | None = None
    three_import_map_injected: bool | None = None  # bare `from 'three'` got MC's import map
    html_bytes: int | None = None
    quiet_box: Literal["ok", "busy", "timeout"] | None = None
    serving_age_min: int | None = None
    recorded_at: datetime | None = None
    harvested_at: datetime | None = None
    unknown: dict[str, str] = Field(default_factory=dict)


class ContestantResult(BaseModel):
    key: str
    runtime_id: uuid.UUID | None = None
    served_model_id: str | None = None
    display_name: str
    alias: str | None = None
    locality: Locality | None = None
    device_name: str | None = None
    harness: Harness | None = None
    effort: EffortRecord
    runs: list[RunView]
    summary: Summary
    receipt: Receipt | None = None
    lib_track: LibTrack = "none"
    receipt_version: int | None = None
    receipt_checksum: str | None = None


class ResultBlock(BaseModel):
    """One class — never mixed (§5.1). ``starts_after`` = B waits for A."""
    run_class: RunClass
    contestants: list[ContestantResult]
    starts_after: RunClass | None = None


class GateReason(BaseModel):
    rule: GateRule
    severity: Literal["red", "amber"]
    code: str
    params: dict[str, Any] = Field(default_factory=dict)
    # The fix the UI offers next to a red reason.
    fix: Literal["harvest", "rerun", "rerun_larger"] | None = None


class Gate(BaseModel):
    status: GateStatus
    reasons: list[GateReason] = Field(default_factory=list)
    # sha256 over the receipts the artefacts were built from — every
    # artefact carries it (W1.7 acceptance).
    checksum: str | None = None


class TruncationWarning(BaseModel):
    """PRIVATE warning for the operator (decision 24.10.): runs that stopped at max_tokens.
    Never printed on public artefacts. The result page offers
    "re-run with a larger budget" before the kit is downloaded."""
    runs_cut: int
    n_runs: int
    max_tokens: int
    suggested_max_tokens: int
    # A capped run was cut, so a re-run with more tokens exists. False when
    # only local runs were cut: they already had the whole context window.
    larger_budget: bool = True
    entry_ids: list[uuid.UUID] = Field(default_factory=list)
    # Runs that stopped before they wrote any page (thinking until the limit).
    runs_without_page: int = 0
    # Run 1 (the video run) was cut: the video shows an unfinished page.
    video_run_cut: bool = False


class Verdict(BaseModel):
    """The verdict line's facts — the UI builds the sentence (i18n)."""
    served_model_id: str
    run_class: RunClass
    n_runs: int
    time: Stat
    tokens_out: Stat
    page_check: Literal["loads", "blank", "unknown"] | None = None


class VideoRef(BaseModel):
    path: str                         # absolute /shared-deliverables/... (files API)
    duration_s: float | None = None
    run_index: int = 1


class OverlayAction(BaseModel):
    """ADR-044 extension point (catalog publisher) — unchanged shape."""
    id: str
    label: str
    style: Literal["default", "primary", "danger"] = "default"
    method: Literal["POST"] = "POST"
    endpoint: str
    confirm: str | None = None
    disabled: bool = False
    disabled_reason: str | None = None
    busy: bool = False


class BenchTestDetail(BaseModel):
    """GET /bench/tests/{id} — result page AND live run (poll while
    ``state`` is waiting/running)."""
    id: uuid.UUID
    title: str
    created_at: datetime
    trigger: Trigger | None = None
    preset: PresetId | None = None
    protocol_version: Literal["v0", "v1"] = "v1"
    state: BenchTestState
    section: Section
    state_reason: Reason | None = None
    prompt: PromptRef
    blocks: list[ResultBlock]
    gate: Gate
    verdict: Verdict | None = None
    video: VideoRef | None = None
    truncation: TruncationWarning | None = None
    rerun_of: uuid.UUID | None = None
    # Which buttons may show: stop | rerun | rerun_larger | harvest | kit |
    # rerender | recompose | archive | delete
    allowed: list[str] = Field(default_factory=list)
    overlay_actions: list[OverlayAction] = Field(default_factory=list)


class BenchTestListContestant(BaseModel):
    display_name: str
    harness: Harness | None = None
    locality: Locality | None = None


class BenchTestListItem(BaseModel):
    id: uuid.UUID
    title: str
    created_at: datetime
    section: Section
    state: BenchTestState
    state_reason: Reason | None = None
    run_class: RunClass | None = None
    contestants: list[BenchTestListContestant]
    progress: dict[str, int] | None = None    # {"done": 2, "total": 3} while running
    gate_status: GateStatus = "pending"


class BenchTestList(BaseModel):
    items: list[BenchTestListItem]
    counts: dict[Section, int]
    total: int
    last_at: datetime | None = None


class SeenResponse(BaseModel):
    """POST /bench/tests/{id}/seen — Eingang items resolved."""
    resolved: int = 0


class RerunRequest(BaseModel):
    budget: Literal["same", "larger"] = "same"
    # D4 on the re-run route too: a paid model answers 409 CostConfirmation;
    # the page confirms here (same shape as BenchTestCreate) so the larger
    # budget of the truncation re-run is never lost on a detour.
    cost_confirmed_usd: float | None = Field(default=None, ge=0)
    confirm_over_cap: bool = False
    confirm_unknown_cost: bool = False


# ── X-Kit ────────────────────────────────────────────────────────────────


class KitFile(BaseModel):
    name: KitFileName
    ready: bool
    bytes: int | None = None
    width: int | None = None
    height: int | None = None
    duration_s: float | None = None


class KitTexts(BaseModel):
    """English public texts (decision 24.10.). Facts only from green
    numbers, unranked; never-print list applies (§6.4)."""
    settings: list[str]          # disclosure blocks, each ≤ 280 chars
    alt_text: str = Field(max_length=1000)
    facts: list[str]
    link: str | None = None      # only when an episode is published
    thread_guide: list[str]      # main post · reply 1 · reply 2


class XKit(BaseModel):
    test_id: uuid.UUID
    gate: Gate
    # Red = preview only (blocked_reason), files not downloadable.
    downloadable: bool
    blocked_reason: Reason | None = None
    files: list[KitFile]
    texts: KitTexts
    footer: str                  # handle from config (MC_BENCH_FOOTER)
    # The private warning is repeated here so the sheet can stop the operator
    # before "Alles laden" (24.10.).
    truncation: TruncationWarning | None = None


class DownloadToken(BaseModel):
    token: str
    expires_in: int


# ── Entries (Runtimes cards, auto test after a switch) ───────────────────


class PlannedTest(BaseModel):
    id: uuid.UUID
    not_before: datetime | None = None


class RuntimeTestSummary(BaseModel):
    """GET /bench/runtime-summary — one row per runtime for the Runtimes
    page "Testen" buttons (P1). ``primary``: only the most recently
    started live card gets the primary style (K11)."""
    runtime_id: uuid.UUID
    testable: bool
    reason: Reason | None = None         # why not ("box_asleep", "no_model")
    primary: bool = False
    last_test: LastTest | None = None
    running_test_id: uuid.UUID | None = None
    # An auto test after a recipe switch that has no runs yet (it waits for
    # the model): the card says "Test geplant · 11:45" instead of offering a
    # second test on the same model.
    planned_test: PlannedTest | None = None


class AutoTestCreate(BaseModel):
    """POST /bench/auto-tests — sent by the recipe switch dialog right after
    a successful switch with the toggle on (P1b, W1.11)."""
    host_id: uuid.UUID
    recipe_slug: str = Field(min_length=1, max_length=128)


class AutoTestResponse(BaseModel):
    test_id: uuid.UUID
    state: BenchTestState
    not_before: datetime | None = None
    # A pending auto test of the same host that this one replaced.
    superseded_test_id: uuid.UUID | None = None
