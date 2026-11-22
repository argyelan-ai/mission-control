/**
 * Content widths inside AppShell's column — one shared left edge.
 *
 * AppShell pads its column (px-4 · md:px-6 · lg:px-8); a page starts exactly
 * there and caps only its width, never centres itself with `mx-auto`. A
 * centred narrow page started at a different x than its neighbours, so the
 * title jumped sideways when moving between pages (DESIGN.md K9).
 *
 *   PAGE_READ — lists, forms, text (inbox, More)
 *   PAGE_WIDE — tables and dashboards (insights, schedule, files)
 */
export const PAGE_READ = "w-full max-w-3xl";
export const PAGE_WIDE = "w-full max-w-6xl";
