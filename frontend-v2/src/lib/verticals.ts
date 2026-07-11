// Vertical flags — the public release build strips vertical directories
// and sets these flags to false (scripts/release-public.sh).
export const VERTICALS = { newsStudio: false, benchStudio: true } as const;
