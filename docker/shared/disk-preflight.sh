# disk-preflight.sh — free-space gate in front of EVERY build, plus build-cache
# cleanup behind it.
#
# Why this exists (measured, 2026-09-16): the box filled up. `docker system df`
# showed 75.8 GB of build cache — Docker never garbage-collects it by default.
# `docker compose up --build` then died mid-build with an opaque
# "no space left on device", after minutes of work, and the operator's only clue
# was the raw docker error. The log-cap fix (CHANGELOG: "Container logs are
# capped (10 MB × 3 files per service)") treated one writer; the build cache was
# the actual cause and had no ceiling at all.
#
# Two halves, both here so every entry point behaves identically:
#   mc_disk_preflight      — refuse the build when free space is below the
#                            threshold, with a message that names the numbers,
#                            where the threshold came from, and how to lower it.
#   mc_build_cache_cleanup — after a SUCCESSFUL build, prune the build cache
#                            back under its own ceiling. Only ever on success:
#                            wiping the cache when a build failed destroys the
#                            layer cache the retry needs.
#
# POSIX-sh compatible (no bashisms, no `local`): sourced by bash entry points
# (install.sh, start-all.sh, build-agent-images.sh) AND by scripts running under
# BusyBox ash. No shebang, same as docker/shared/recycler-lib.sh — this is a
# library, the consumer owns the interpreter. Every internal name is `_mc_`
# prefixed because without `local` those names stay in the caller's scope.
#
# Threshold sources, first hit wins:
#   1. $BUILD_MIN_FREE_GB in the environment (explicit override, wins)
#   2. BUILD_MIN_FREE_GB=... in $MC_ENV_FILE (default .env — the project's
#      config path, where the operator already keeps their other settings)
#   3. MC_BUILD_MIN_FREE_GB_DEFAULT below (shipped default)
# A threshold baked only into the code would be invisible to whoever runs on a
# small box; this keeps it in the project's config path.
#
# The NAME is `BUILD_MIN_FREE_GB`, not `MC_BUILD_MIN_FREE_GB`, and that is
# load-bearing: `config.py:build_min_free_gb`, the docker-compose pass-through
# and .env.example all use it. The Python and shell implementations read the
# same variable on purpose — an `MC_`-prefixed shell-side name would silently
# ignore the one the operator is told to set, which is the same silent-drift
# bug this card fixes elsewhere.

# Shipped default. Sized so a from-scratch `docker compose up --build` (backend
# + frontend + cdp-browser, roughly 10-15 GB of layers on a cold cache) has room
# to finish, with headroom for the concurrent agent-image builds.
MC_BUILD_MIN_FREE_GB_DEFAULT=15

# Ceiling the build cache is pruned back to after a build. Without it the cache
# is unbounded — that is the 75.8 GB above.
MC_BUILD_CACHE_KEEP_GB_DEFAULT=20

# _mc_env_value <KEY> — value of KEY from $MC_ENV_FILE (default: ./.env), empty
# when the file or the key is absent. Surrounding quotes are stripped.
#
# Reads the file instead of sourcing it: `.env` is data, and sourcing it would
# execute whatever someone typed into a value. Same reason install.sh greps it.
_mc_env_value() {
    _mc_env_file="${MC_ENV_FILE:-.env}"
    [ -f "$_mc_env_file" ] || return 0
    _mc_env_raw=$(sed -n "s/^$1=//p" "$_mc_env_file" 2>/dev/null | tail -n 1)
    case "$_mc_env_raw" in
        \"*\") _mc_env_raw=${_mc_env_raw#\"}; _mc_env_raw=${_mc_env_raw%\"} ;;
        \'*\') _mc_env_raw=${_mc_env_raw#\'}; _mc_env_raw=${_mc_env_raw%\'} ;;
    esac
    printf '%s' "$_mc_env_raw"
}

# _mc_positive_int <key> <value> — echo <value> if it is a plain non-negative
# integer, else echo nothing. `[ a -lt b ]` on "15.5" or "abc" is a hard shell
# error in the middle of a build; a bad threshold must not become a crash.
_mc_positive_int() {
    case "$2" in
        ''|*[!0-9]*) return 0 ;;
    esac
    printf '%s' "$2"
}

# mc_build_min_free_gb — the threshold in GB. Always prints a usable number.
mc_build_min_free_gb() {
    _mc_min=$(_mc_positive_int BUILD_MIN_FREE_GB "${BUILD_MIN_FREE_GB:-}")
    [ -n "$_mc_min" ] || _mc_min=$(_mc_positive_int BUILD_MIN_FREE_GB "$(_mc_env_value BUILD_MIN_FREE_GB)")
    [ -n "$_mc_min" ] || _mc_min="$MC_BUILD_MIN_FREE_GB_DEFAULT"
    printf '%s' "$_mc_min"
}

# mc_build_cache_keep_gb — how much build cache survives a cleanup.
mc_build_cache_keep_gb() {
    _mc_keep=$(_mc_positive_int BUILD_CACHE_KEEP_GB "${BUILD_CACHE_KEEP_GB:-}")
    [ -n "$_mc_keep" ] || _mc_keep=$(_mc_positive_int BUILD_CACHE_KEEP_GB "$(_mc_env_value BUILD_CACHE_KEEP_GB)")
    [ -n "$_mc_keep" ] || _mc_keep="$MC_BUILD_CACHE_KEEP_GB_DEFAULT"
    printf '%s' "$_mc_keep"
}

# mc_free_gb <path> — whole free GB at <path>, or empty when unreadable.
#
# `df -Pk` is POSIX; `df -h --output=avail` is GNU-only and dies on BSD/macOS
# (same reasoning as services/host_probe.py). Integer GB is enough for a
# threshold and keeps the comparison plain `[ -lt ]` arithmetic — and it rounds
# DOWN, so 14.9 GB free reads as 14 and fails a 15 GB threshold: the safe
# direction for a refusal.
mc_free_gb() {
    _mc_df_out=$(df -Pk "$1" 2>/dev/null | awk 'NR==2 {print $4}')
    case "$_mc_df_out" in
        ''|*[!0-9]*) printf '%s' "" ; return 0 ;;
    esac
    printf '%s' $(( _mc_df_out / 1048576 ))
}

# mc_disk_preflight [path] — refuse to build below the threshold.
#
# Returns 0 to build, 1 to stop. The reason goes to stderr so a caller that
# swallows stdout (`start-all.sh` pipes compose output through grep) still shows
# it to the operator.
mc_disk_preflight() {
    _mc_pf_path="${1:-/}"
    _mc_pf_min=$(mc_build_min_free_gb)
    _mc_pf_free=$(mc_free_gb "$_mc_pf_path")

    if [ -z "$_mc_pf_free" ]; then
        # Unknown is not zero. `df` missing or the path unreadable (exotic
        # filesystem, container without the mount) is a measurement failure;
        # refusing a build over it would invent a problem that is not proven.
        echo "⚠ Plattenplatz-Preflight: freier Platz an $_mc_pf_path nicht lesbar — Bau laeuft weiter (Pruefung uebersprungen)." >&2
        return 0
    fi

    if [ "$_mc_pf_free" -lt "$_mc_pf_min" ]; then
        {
            echo ""
            echo "✗ Bau abgebrochen: zu wenig Plattenplatz."
            echo "  Frei an $_mc_pf_path: ${_mc_pf_free} GB — noetig: ${_mc_pf_min} GB."
            echo "  Schwelle ${_mc_pf_min} GB (aus BUILD_MIN_FREE_GB in der Umgebung oder"
            echo "  .env; Standard ${MC_BUILD_MIN_FREE_GB_DEFAULT} GB)."
            echo "  Ein Bau ohne Platz stirbt mitten im Layer-Schreiben mit einem rohen"
            echo "  \"no space left on device\" — deshalb bricht er hier ab."
            # The bound in this hint is the CACHE ceiling, not the free space.
            # `--keep-storage <free>g` would tell the operator to keep 373 GB of
            # cache on the very disk that just ran out — the exact opposite of
            # the fix, and it reads as authoritative because it is a real number
            # measured from this machine. `mc_build_cache_keep_gb` is the value
            # that actually bounds the cache, and it follows the operator's own
            # configuration (BUILD_CACHE_KEEP_GB) instead of a literal here.
            echo "  Platz schaffen:  docker builder prune --keep-storage $(mc_build_cache_keep_gb)g"
            echo "  Schwelle senken: BUILD_MIN_FREE_GB=<GB> (in .env auch dauerhaft)."
            echo ""
        } >&2
        return 1
    fi

    echo "→ Plattenplatz-Preflight: ${_mc_pf_free} GB frei (Schwelle ${_mc_pf_min} GB)."
    return 0
}

# mc_build_cache_cleanup — prune the build cache back under its ceiling.
#
# Call ONLY after a successful build (see the header). Never changes the exit
# status of the build that called it: a cache problem after a green build is not
# a build failure, and turning it into one would fail a build that already
# produced its image.
mc_build_cache_cleanup() {
    _mc_cc_keep=$(mc_build_cache_keep_gb)
    if command -v docker >/dev/null 2>&1; then
        if docker builder prune --force --keep-storage "${_mc_cc_keep}g" >/dev/null 2>&1; then
            echo "→ Build-Cache auf ${_mc_cc_keep} GB begrenzt."
        else
            echo "⚠ Build-Cache-Aufraeumen fehlgeschlagen (Bau selbst war erfolgreich)." >&2
        fi
    fi
    return 0
}
