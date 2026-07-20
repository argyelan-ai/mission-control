# ui-detect.sh — Detect which CLI runtime is rendered in tmux Window 0.
#
# Bug 14 (2026-05-13): paste_and_submit sends a bracketed-paste end-marker
# (`\e[201~`) after `tmux paste-buffer` to make sure claude-cli leaves
# paste-mode before the Enter that triggers submit. claude-cli NEEDS that
# marker. openclaude does NOT — it sees the marker as literal text and
# breaks the submit. Result: Sparky (openclaude) saw `paste_and_submit`
# claim success while the input never landed.
#
# detect_pane_ui SESSION_TARGET
#   Captures the last 8 lines of the given pane and echoes one of:
#     "claude"     — claude-cli rendered (box-glyphs `╭─` / `╰─`)
#     "openclaude" — openclaude rendered (`❯ ` prompt or `bypass permissions`)
#     ""           — could not determine
#   Returns 0 on a positive match, 1 if undetermined.
#
# Detection order: claude pattern first, openclaude second. Box-glyphs are
# more specific than the bare `❯ ` (which can appear in other shells), so
# they win when both are visible (e.g. inside claude-cli's input prompt).
detect_pane_ui() {
    local target="$1"
    # Live pilot finding 2026-07-20: claude-cli 2.1.x dropped the box glyphs
    # (no more ╭─/╰─) and renders `❯` + horizontal lines — visually the same
    # as openclaude, so the pane heuristic below misdetects claude as
    # openclaude and paste_and_submit skips the bracketed-paste end-marker
    # claude NEEDS (the submit-Enter degrades to a bare newline, the paste
    # sits unsubmitted in the input). The image knows its runtime with
    # certainty: PANE_UI_OVERRIDE (ENV baked into mc-claude-agent /
    # mc-agent-base, overridable via compose) wins over the heuristic.
    # The heuristic stays as fallback for host agents without the ENV.
    if [ -n "${PANE_UI_OVERRIDE:-}" ]; then
        echo "$PANE_UI_OVERRIDE"
        return 0
    fi
    local pane
    pane=$(tmux capture-pane -t "$target" -p 2>/dev/null || echo "")
    if [ -z "$pane" ]; then
        echo ""
        return 1
    fi
    local tail8
    tail8=$(echo "$pane" | tail -8)
    if echo "$tail8" | grep -qE '╭─|╰─' 2>/dev/null; then
        echo "claude"
        return 0
    fi
    if echo "$tail8" | grep -qE '^❯ *$|bypass permissions' 2>/dev/null; then
        echo "openclaude"
        return 0
    fi
    echo ""
    return 1
}
