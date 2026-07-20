# paste-verify.sh — Post-paste verification heuristic for poll.sh.
#
# Bug 10 (2026-05-13): paste_and_submit used to be silent-fail — when the
# tmux paste-buffer raced with openclaude's pty (most often during a quick
# re-dispatch after Re-Open/Review-Rejection), the input never landed in
# the pane. The log said "paste trotzdem (fail-open)" and claude sat idle.
#
# verify_paste_landed FILE
#   Extracts the first non-empty line of FILE (clipped to PASTE_FINGERPRINT_LEN
#   chars) and checks via `tmux capture-pane` whether that fingerprint shows
#   up in the most recent 100 lines of pane scrollback. Returns 0 if the
#   fingerprint is found OR if no fingerprint could be extracted (optimistic
#   fallback for edge cases like empty files); 1 otherwise.
#
# Internal probe-loop (Bug 12 fix, 2026-05-13): performs PASTE_PROBE_ATTEMPTS
# capture-pane probes with PASTE_PROBE_INTERVAL_SEC gaps. Reduces false-
# negatives when openclaude renders the paste a beat later than expected
# (live-bug 2026-05-13 sparky: verify said miss but paste landed 2s later).
#
# Plus: progressive fingerprint shrinking. claude wraps long lines at the
# terminal width and adds box-border glyphs in the middle. So if the full
# fingerprint misses, we retry with shorter prefixes (50%, 25% of full).
#
# Bug 16 fix (2026-05-14): scrollback widened from -S -100 to -S -2000.
# Long dispatch prompts (Voice-Foundation context + code + memory >200 lines)
# push the fingerprint (first line of paste) out of the -100 window because
# claude wraps + renders extras above. Result was: paste landed correctly
# but verify_paste_landed returned 1 → unnecessary retry → second paste hit
# the running cook. -S -2000 matches tmux default history-limit and covers
# virtually any prompt.
#
# Required env:
#   SESSION_NAME — tmux session name (poll.sh sets this from AGENT_NAME)
#
# Tunables (with defaults):
#   PASTE_FINGERPRINT_LEN     (40)    — clip the first line to this many chars
#   PASTE_PROBE_ATTEMPTS      (3)     — how many capture-pane probes per call
#   PASTE_PROBE_INTERVAL_SEC  (1)     — sleep between probes
#   PASTE_SCROLLBACK_LINES    (2000)  — capture-pane -S window depth

verify_paste_landed() {
    local file="$1"
    local full
    full=$(grep -v '^$' "$file" 2>/dev/null | head -n 1 | cut -c1-"${PASTE_FINGERPRINT_LEN:-40}")
    if [ -z "$full" ]; then
        return 0
    fi
    # Progressive fingerprints: full, then 50%, then 25%. Three lengths total.
    local len_full=${#full}
    local len_half=$(( len_full / 2 ))
    local len_quarter=$(( len_full / 4 ))
    [ "$len_half" -lt 8 ] && len_half=$len_full
    [ "$len_quarter" -lt 8 ] && len_quarter=$len_half
    local fp_half="${full:0:$len_half}"
    local fp_quarter="${full:0:$len_quarter}"

    local attempt=1
    local max_attempts=${PASTE_PROBE_ATTEMPTS:-3}
    local interval=${PASTE_PROBE_INTERVAL_SEC:-1}
    local scrollback=${PASTE_SCROLLBACK_LINES:-2000}
    local collapse_tail=${PASTE_COLLAPSE_TAIL_LINES:-40}
    while [ "$attempt" -le "$max_attempts" ]; do
        local pane
        pane=$(tmux capture-pane -t "${SESSION_NAME}:0" -p -S "-${scrollback}" 2>/dev/null || echo "")
        if [ -n "$pane" ]; then
            # Try full -> half -> quarter. Any match counts.
            if echo "$pane" | grep -qF "$full" 2>/dev/null \
               || echo "$pane" | grep -qF "$fp_half" 2>/dev/null \
               || echo "$pane" | grep -qF "$fp_quarter" 2>/dev/null; then
                return 0
            fi
        fi
        # claude-cli >= 2.x collapses multi-line pastes to "[Pasted text #N
        # +M lines]" — the content never renders in the pane, so no
        # fingerprint length can ever match (live pilot finding 2026-07-20:
        # every comm_v2 message flush failed verify although the paste
        # landed, queue wedged, endless redelivery; the dispatch path
        # double-pasted for the same reason). A collapse marker counts as
        # landed — but only if the marker COUNT in the tail window (input
        # area + freshest turn) grew vs. the pre-paste snapshot
        # (PASTE_PRE_COLLAPSE_COUNT, set by paste_and_submit). A stale
        # marker from an earlier paste never increases the count, so
        # back-to-back queue flushes can't false-ack an undelivered
        # message. Unset snapshot (direct callers, tests) degrades to 0 —
        # any visible marker counts.
        local tail_pane marker_count
        tail_pane=$(tmux capture-pane -t "${SESSION_NAME}:0" -p -S "-${collapse_tail}" 2>/dev/null || echo "")
        if [ -n "$tail_pane" ]; then
            marker_count=$(printf '%s\n' "$tail_pane" | grep -cF '[Pasted text' 2>/dev/null || true)
            [ -n "$marker_count" ] || marker_count=0
            if [ "$marker_count" -gt "${PASTE_PRE_COLLAPSE_COUNT:-0}" ]; then
                return 0
            fi
        fi
        if [ "$attempt" -lt "$max_attempts" ]; then
            sleep "$interval"
        fi
        attempt=$((attempt + 1))
    done
    return 1
}
