// Vertical flags — the public release build strips vertical directories
// and sets these flags to false (scripts/release-public.sh).
// Default build: the bench studio is enabled.
export const VERTICALS = { benchStudio: true } as const;
