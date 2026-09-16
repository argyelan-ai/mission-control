#!/usr/bin/env bash
# Behavior + sabotage tests for docker/shared/disk-preflight.sh.
#
# The card's acceptance criteria are behavioral ("Bau verweigert / Bau laeuft",
# "meldet / schweigt"), so every test drives the REAL library through its
# public functions with a REAL process exit status — no mocks for the thing
# under test. Only `df` and `docker` are stubbed, because a test harness that
# needs 376 GB of real free space (or a Docker daemon) is not a test anyone can
# run.
#
# Sabotage probes (one disarmed spot per test, per the card's DoD). Each names
# the ONE line to undo in disk-preflight.sh; disarming it must turn exactly
# that test red:
#
#   refuse  : the `[ "$_mc_pf_free" -lt "$_mc_pf_min" ]` comparison inverted
#             (or the `return 1` replaced with `return 0`) → "Bau verweigert"
#             cannot fail, and a full disk builds anyway
#   allow   : the comparison replaced with a bare `return 1` → the gate refuses
#             EVERY build, including the normal run
#   unknown : the `[ -z "$_mc_pf_free" ]` branch changed to `return 1` →
#             an unreadable `df` (exotic FS) blocks every build
#   envread : `_mc_env_value` replaced with `printf '%s' ""` → the .env
#             threshold is ignored and the shipped default silently applies
#   prune   : the `--keep-storage "${_mc_cc_keep}g"` flag dropped from the
#             `docker builder prune` call → the cache is wiped instead of
#             bounded, i.e. exactly the bug this card exists to fix
#   status  : `return 0` at the end of mc_build_cache_cleanup removed → a
#             cache failure after a green build turns the build red
#
# Runs under bash AND BusyBox ash: the lib is sourced by install.sh (bash) and
# by scripts running in alpine (sh). The harness therefore stays POSIX itself
# (`set -u`, no pipefail — the pipeline-safe flag is a bashism and dash rejects
# it outright), so `sh test_disk_preflight.sh` exercises the lib on the strict
# interpreter rather than on bash wearing a different hat.
set -u

# Hermetic against the caller's environment. CI sets BUILD_MIN_FREE_GB on the
# build steps of this same job (hosted runners have ~14 GB free, so the local
# 15 GB default would falsely refuse); if that leaked in here, the `.env` test
# below would measure the ambient variable instead of the file and report a
# false failure. Every test states its own threshold inline.
unset BUILD_MIN_FREE_GB BUILD_CACHE_KEEP_GB MC_ENV_FILE

HERE="$(cd "$(dirname "$0")" && pwd)"
LIB="${DISK_PREFLIGHT_BIN:-$HERE/disk-preflight.sh}"

PASS=0
FAIL=0

pass() { echo "PASS: $*"; PASS=$((PASS + 1)); }
fail() { echo "FAIL: $*"; FAIL=$((FAIL + 1)); }

# ── Stubs ────────────────────────────────────────────────────────────────────
# A throwaway PATH prefix holding `df` and `docker`. Both read canned values
# from the environment so each test states its own scenario in one line.
STUB="$(mktemp -d "${TMPDIR:-/tmp}/disk-preflight-test.XXXXXX")"
cleanup() { rm -rf "$STUB"; }
trap cleanup EXIT

# df stub: prints MC_TEST_DF_OUT verbatim, or exits MC_TEST_DF_RC.
cat > "$STUB/df" <<'STUB_EOF'
#!/bin/sh
if [ "${MC_TEST_DF_RC:-0}" -ne 0 ]; then
    echo "df: cannot read table of mounted file systems" >&2
    exit "${MC_TEST_DF_RC}"
fi
if [ "${MC_TEST_DF_OUT:-}" = "__EMPTY__" ]; then
    exit 0
fi
printf '%s\n' "${MC_TEST_DF_OUT}"
STUB_EOF
chmod +x "$STUB/df"

# docker stub: records its argv so a test can assert the prune was BOUNDED, and
# can be told to fail. A stub that only "does not crash" would pass even if the
# library wiped the whole cache.
cat > "$STUB/docker" <<'STUB_EOF'
#!/bin/sh
printf '%s\n' "$*" >> "${MC_TEST_DOCKER_LOG:-/dev/null}"
exit "${MC_TEST_DOCKER_RC:-0}"
STUB_EOF
chmod +x "$STUB/docker"

PATH="$STUB:$PATH"
export PATH

# df -Pk output with N free 1K-blocks. Column 4 is `Available`; the values here
# are small so the arithmetic is checkable by eye (1048576 blocks = 1 GB).
df_out_with_free_gb() {
    printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sda1 100000000 %s %s 54%% /\n' \
        "$((10485760 - $1 * 1048576))" "$(( $1 * 1048576 ))"
}

# ── Test 1: normal run — enough space, the build proceeds ────────────────────
test_allow_when_space_available() {
    MC_TEST_DF_OUT="$(df_out_with_free_gb 40)" \
    BUILD_MIN_FREE_GB=15 \
        sh -c '. "$1"; mc_disk_preflight /' sh "$LIB" >/dev/null 2>&1
    if [ $? -eq 0 ]; then
        pass "genug Platz (40 GB frei, Schwelle 15 GB) → Bau laeuft (exit 0)"
    else
        fail "genug Platz, aber Preflight verweigert — Sabotage-Kandidat: 'allow' in $LIB"
    fi
}

# ── Test 2: refuse — below the threshold ─────────────────────────────────────
test_refuse_when_space_low() {
    MC_TEST_DF_OUT="$(df_out_with_free_gb 3)" \
    BUILD_MIN_FREE_GB=15 \
        sh -c '. "$1"; mc_disk_preflight /' sh "$LIB" >/dev/null 2>&1
    if [ $? -eq 1 ]; then
        pass "zu wenig Platz (3 GB frei, Schwelle 15 GB) → Bau verweigert (exit 1)"
    else
        fail "zu wenig Platz, aber Preflight liess den Bau durch — Sabotage-Kandidat: 'refuse' in $LIB"
    fi
}

# ── Test 3: the refusal message names the numbers and the fix ────────────────
# Criterion 1 asks for a "klare Meldung", not merely a non-zero status. Both
# numbers, the source of the threshold and the remedy must be in it: the whole
# point of the card is that the raw "no space left on device" told the operator
# nothing.
test_refusal_message_is_actionable() {
    MSG=$(MC_TEST_DF_OUT="$(df_out_with_free_gb 3)" BUILD_MIN_FREE_GB=15 \
        sh -c '. "$1"; mc_disk_preflight / 2>&1 >/dev/null' sh "$LIB")

    MISSING=""
    case "$MSG" in *"3 GB"*) ;; *) MISSING="$MISSING freier-Platz-Zahl" ;; esac
    case "$MSG" in *"15 GB"*) ;; *) MISSING="$MISSING Schwelle-Zahl" ;; esac
    case "$MSG" in *"BUILD_MIN_FREE_GB"*) ;; *) MISSING="$MISSING Schwellen-Quelle" ;; esac
    case "$MSG" in *"builder prune"*) ;; *) MISSING="$MISSING Handlungsanweisung" ;; esac

    if [ -z "$MISSING" ]; then
        pass "Verweigerungs-Meldung nennt beide Zahlen, die Quelle und den Fix"
    else
        fail "Meldung unvollstaendig, fehlt:$MISSING — Meldung war: $MSG"
    fi
}

# ── Test 4: the reason goes to stderr, not stdout ────────────────────────────
# start-all.sh pipes compose's stdout through `grep -E "Started|Health"`; a
# refusal printed on stdout would be swallowed by that filter and the operator
# would see an empty start with no explanation.
test_refusal_goes_to_stderr() {
    OUT=$(MC_TEST_DF_OUT="$(df_out_with_free_gb 3)" BUILD_MIN_FREE_GB=15 \
        sh -c '. "$1"; mc_disk_preflight / 2>/dev/null' sh "$LIB")
    if [ -z "$OUT" ]; then
        pass "Verweigerung schreibt auf stderr (stdout bleibt leer → grep-Filter verschluckt sie nicht)"
    else
        fail "Verweigerung landete auf stdout und wuerde von start-all.shs grep verschluckt: $OUT"
    fi
}

# ── Test 5: unreadable df must NOT block the build ───────────────────────────
# Unknown is not zero. A `df` that cannot read the filesystem is a measurement
# failure; refusing there would invent a problem that was never proven, and on
# an exotic mount it would block every build forever.
test_unknown_space_allows_build() {
    MC_TEST_DF_RC=1 BUILD_MIN_FREE_GB=15 \
        sh -c '. "$1"; mc_disk_preflight /' sh "$LIB" >/dev/null 2>&1
    if [ $? -eq 0 ]; then
        pass "df nicht lesbar → Bau laeuft weiter (exit 0), nur Warnung"
    else
        fail "unlesbares df blockierte den Bau — Sabotage-Kandidat: 'unknown' in $LIB"
    fi
}

# ── Test 6: an empty/garbage df must also not block ──────────────────────────
test_garbage_df_allows_build() {
    MC_TEST_DF_OUT="__EMPTY__" BUILD_MIN_FREE_GB=15 \
        sh -c '. "$1"; mc_disk_preflight /' sh "$LIB" >/dev/null 2>&1
    if [ $? -eq 0 ]; then
        pass "df liefert keine Datenzeile → Bau laeuft weiter (exit 0)"
    else
        fail "leere df-Ausgabe blockierte den Bau (Parser meldete 0 GB statt 'unbekannt')"
    fi
}

# ── Test 7: threshold comes from .env ────────────────────────────────────────
# The card forbids a hardcoded threshold: it must come through the project's
# config path. A threshold of 40 in .env with 20 GB free must refuse, while the
# shipped default (15) would have allowed it — the test proves the .env value
# is what decided.
test_threshold_read_from_env_file() {
    ENV_FILE="$STUB/test.env"
    printf 'BUILD_MIN_FREE_GB=40\n' > "$ENV_FILE"

    MC_TEST_DF_OUT="$(df_out_with_free_gb 20)" MC_ENV_FILE="$ENV_FILE" \
        sh -c '. "$1"; mc_disk_preflight /' sh "$LIB" >/dev/null 2>&1
    RC=$?
    rm -f "$ENV_FILE"

    if [ $RC -eq 1 ]; then
        pass ".env-Schwelle (40 GB) greift: 20 GB frei → verweigert (Default 15 haette erlaubt)"
    else
        fail ".env-Schwelle wurde ignoriert (20 GB frei, .env-Schwelle 40) — Sabotage-Kandidat: 'envread' in $LIB"
    fi
}

# ── Test 8: the exported threshold WINS over .env ────────────────────────────
# Explicit environment beats the file, so a one-off run can lower the bar
# without editing a tracked file.
test_env_var_overrides_env_file() {
    ENV_FILE="$STUB/test2.env"
    printf 'BUILD_MIN_FREE_GB=40\n' > "$ENV_FILE"

    MC_TEST_DF_OUT="$(df_out_with_free_gb 20)" BUILD_MIN_FREE_GB=5 \
    MC_ENV_FILE="$ENV_FILE" \
        sh -c '. "$1"; mc_disk_preflight /' sh "$LIB" >/dev/null 2>&1
    RC=$?
    rm -f "$ENV_FILE"

    if [ $RC -eq 0 ]; then
        pass "BUILD_MIN_FREE_GB=5 schlaegt .env (40): 20 GB frei → Bau laeuft"
    else
        fail "env-Variable schlug die .env nicht — der explizite Override muss gewinnen"
    fi
}

# ── Test 9: a non-numeric threshold must not crash the shell ─────────────────
# `[ 15.5 -lt 20 ]` is a hard error under `set -e` in the middle of a build.
test_non_numeric_threshold_is_ignored() {
    MC_TEST_DF_OUT="$(df_out_with_free_gb 40)" BUILD_MIN_FREE_GB="15.5" \
        sh -c '. "$1"; mc_disk_preflight /' sh "$LIB" >/dev/null 2>&1
    if [ $? -eq 0 ]; then
        pass "kaputte Schwelle ('15.5') faellt auf den Default zurueck statt zu crashen"
    else
        fail "nicht-numerische Schwelle liess den Preflight scheitern (Shell-Fehler mitten im Bau)"
    fi
}

# ── Test 10: cleanup BOUNDS the cache, it does not wipe it ───────────────────
# The regression this card exists for: an unbounded 75.8 GB build cache. A
# cleanup that runs `docker builder prune --force` without --keep-storage wipes
# the cache the next build needs.
test_cleanup_bounds_cache() {
    LOG="$STUB/docker.log"
    : > "$LOG"
    MC_TEST_DOCKER_LOG="$LOG" BUILD_CACHE_KEEP_GB=20 \
        sh -c '. "$1"; mc_build_cache_cleanup' sh "$LIB" >/dev/null 2>&1

    CALL=$(cat "$LOG" 2>/dev/null)
    case "$CALL" in
        *"builder prune"*"--keep-storage 20g"*)
            pass "Cleanup ruft 'docker builder prune --keep-storage 20g' — begrenzt statt geloescht" ;;
        *)
            fail "Cleanup begrenzt den Cache nicht, Aufruf war: '$CALL' — Sabotage-Kandidat: 'prune' in $LIB" ;;
    esac
}

# ── Test 11: a failing cleanup must not fail the build ───────────────────────
# The build already produced its image; a cache-prune error afterwards is not a
# build failure, and reporting it as one would fail a green build.
test_cleanup_failure_does_not_fail_build() {
    MC_TEST_DOCKER_RC=1 MC_TEST_DOCKER_LOG=/dev/null \
        sh -c '. "$1"; mc_build_cache_cleanup' sh "$LIB" >/dev/null 2>&1
    if [ $? -eq 0 ]; then
        pass "fehlgeschlagenes Aufraeumen gibt 0 zurueck (gruener Bau bleibt gruen)"
    else
        fail "Aufraeum-Fehler machte den Bau rot — Sabotage-Kandidat: 'status' in $LIB"
    fi
}

# ── Test 12: cleanup without a docker binary is a silent no-op ───────────────
test_cleanup_without_docker_is_noop() {
    # A PATH holding `sh` but no `docker`: run the library in-process instead of
    # spawning a second shell, so the assertion is about the missing binary and
    # not about a PATH that cannot find its own interpreter (which is what an
    # earlier version of this test measured — exit 127, "sh: command not found").
    OUT=$(PATH="$STUB/empty:/bin:/usr/bin" BUILD_CACHE_KEEP_GB=20 \
        sh -c '. "$1"; mc_build_cache_cleanup; echo "rc=$?"' sh "$LIB" 2>&1)
    case "$OUT" in
        *"rc=0"*) pass "ohne docker-Binary ist das Aufraeumen ein No-Op (exit 0)" ;;
        *) fail "fehlendes docker-Binary liess das Aufraeumen scheitern: $OUT" ;;
    esac
}
mkdir -p "$STUB/empty"

echo "=== disk-preflight.sh behavior tests ($LIB) ==="
test_allow_when_space_available
test_refuse_when_space_low
test_refusal_message_is_actionable
test_refusal_goes_to_stderr
test_unknown_space_allows_build
test_garbage_df_allows_build
test_threshold_read_from_env_file
test_env_var_overrides_env_file
test_non_numeric_threshold_is_ignored
test_cleanup_bounds_cache
test_cleanup_failure_does_not_fail_build
test_cleanup_without_docker_is_noop

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
