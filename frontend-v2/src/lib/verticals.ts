// Vertical flags — the public release build strips vertical directories
// and sets these flags to false (scripts/release-public.sh).
// Private deployment (argyelan): both studios enabled.
export const VERTICALS = { newsStudio: true, benchStudio: true } as const;
