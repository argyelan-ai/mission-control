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
    # Markdown-Syntax strippen (live pilot 2026-07-20): claude rendert die
    # submittete Message — aus "# Neue Nachricht" wird "Neue Nachricht",
    # ein Fingerprint MIT '#' kann im Pane nie erscheinen.
    full=$(grep -v '^$' "$file" 2>/dev/null | head -n 1 | sed 's/^[#>*[:space:]]*//' | cut -c1-"${PASTE_FINGERPRINT_LEN:-40}")
    if [ -z "$full" ]; then
        return 0
    fi
    # Zweiter Anker: die LETZTE nicht-leere Zeile. Bei Queue-Messages ist das
    # der eindeutige Footer "[thread <uuid> · seq <n> · …]" — er ueberlebt
    # das Rendering woertlich und identifiziert genau DIESE Message (der
    # Erstzeilen-Fingerprint ist fuer alle Queue-Messages identisch).
    local last_line
    last_line=$(grep -v '^$' "$file" 2>/dev/null | tail -n 1 | sed 's/^[#>*[:space:]]*//' | cut -c1-"${PASTE_FINGERPRINT_LEN:-40}")
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
            if [ -n "$last_line" ] && [ "$last_line" != "$full" ] \
               && echo "$pane" | grep -qF "$last_line" 2>/dev/null; then
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

# classify_paste_outcome FILE — drei-Wege-Klassifikation des Post-Paste-
# Zustands (Interrupt-Gate fix 2026-09-12). verify_paste_landed antwortete
# nur binär und verschwieg den wichtigsten Live-Fall: das Enter ging in den
# Interrupted-Dialog, der Text stand sichtbar IM EINGABEFELD — der
# Fingerprint matchte dort trotzdem (das Feld ist Teil des Captures), und
# die Meldung "Fingerprint nicht sichtbar" beschrieb nicht, was wirklich
# passiert war.
#
# Gibt zurueck (auf stdout):
#   "0" — abgesendet: Fingerprint im SCROLLBACK (ausserhalb des Feld-Tails)
#         sichtbar, oder Collapse-Marker gewachsen, oder Feld leer + Fingerprint
#         im Tail (Turn hat konsumiert).
#   "2" — im Eingabefeld stehengeblieben: Fingerprint NUR im letzten
#         PASTE_INPUT_TAIL_LINES Zeilen sichtbar, nicht im Rest des Verlaufs.
#   "1" — gar nicht angekommen: Fingerprint nirgends sichtbar.
#
# Dasselbe progressive Shrinking (full/50%/25%) + der last_line-Anker wie
# verify_paste_landed. Kein Probe-Loop: der Aufrufer (paste_and_submit) steuert
# Timing und Retries.
classify_paste_outcome() {
    local file="$1"
    local full
    full=$(grep -v '^$' "$file" 2>/dev/null | head -n 1 | sed 's/^[#>*[:space:]]*//' | cut -c1-"${PASTE_FINGERPRINT_LEN:-40}")
    if [ -z "$full" ]; then
        echo "0"
        return 0
    fi
    local last_line
    last_line=$(grep -v '^$' "$file" 2>/dev/null | tail -n 1 | sed 's/^[#>*[:space:]]*//' | cut -c1-"${PASTE_FINGERPRINT_LEN:-40}")
    local len_full=${#full}
    local len_half=$(( len_full / 2 ))
    local len_quarter=$(( len_full / 4 ))
    [ "$len_half" -lt 8 ] && len_half=$len_full
    [ "$len_quarter" -lt 8 ] && len_quarter=$len_half
    local fp_half="${full:0:$len_half}"
    local fp_quarter="${full:0:$len_quarter}"

    local pane
    pane=$(tmux capture-pane -t "${SESSION_NAME}:0" -p -S "-${PASTE_SCROLLBACK_LINES:-2000}" 2>/dev/null || echo "")
    if [ -z "$pane" ]; then
        echo "1"
        return 0
    fi

    _pane_matches_fp() {
        # $1 = pane text; matcht full/half/quarter + last_line-Anker.
        echo "$1" | grep -qF "$full" 2>/dev/null \
            || echo "$1" | grep -qF "$fp_half" 2>/dev/null \
            || echo "$1" | grep -qF "$fp_quarter" 2>/dev/null \
            || { [ -n "$last_line" ] && [ "$last_line" != "$full" ] \
                 && echo "$1" | grep -qF "$last_line" 2>/dev/null; }
    }

    local input_tail_lines=${PASTE_INPUT_TAIL_LINES:-12}
    local tail_fingerprint="0" tail_pane body
    tail_pane=$(echo "$pane" | tail -n "$input_tail_lines")
    if _pane_matches_fp "$tail_pane"; then
        tail_fingerprint="1"
    fi

    # Collapse-Marker-Pfad (identisch zu verify_paste_landed): nur ein ZUWACHS
    # gegen das Pre-Paste-Snapshot zaehlt.
    local marker_count
    marker_count=$(printf '%s\n' "$pane" | tail -n "${PASTE_COLLAPSE_TAIL_LINES:-40}" | grep -cF '[Pasted text' 2>/dev/null || true)
    [ -n "$marker_count" ] || marker_count=0
    if [ "$marker_count" -gt "${PASTE_PRE_COLLAPSE_COUNT:-0}" ]; then
        echo "0"
        return 0
    fi

    if [ "$tail_fingerprint" = "1" ]; then
        # Fingerprint im Feld-Tail sichtbar — abgesendet oder haengengeblieben?
        # Absendet-Indizien: (a) Fingerprint auch IRGENDWO ausserhalb des Tails
        # (Turn hat den Text in den Verlauf gerendert), oder (b) das Feld ist
        # zwischenzeitlich leer und die letzten Zeilen zeigen den Prompt.
        body=$(echo "$pane" | head -n -"$input_tail_lines")
        if _pane_matches_fp "$body"; then
            echo "0"
            return 0
        fi
        # (b): Feld-Tail ohne den Fingerprint UND ohne Input-Reste ⇒ der Text
        # ist aus dem Feld raus (abgesendet). Achtung false-positive-Gefahr:
        # ohne rendering im Verlauf (collapse-Pfad oben abgedeckt) sind wir
        # konservativ — nur wenn der Fingerprint im Tail WEG ist gilt (b).
        echo "2"
        return 0
    fi
    echo "1"
    return 0
}
