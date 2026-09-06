/**
 * Shared "jump to the Models section and select a tab" mechanics.
 *
 * Lives outside page.tsx on purpose: a Next.js `page.tsx` may only export its
 * default component (plus the handful of framework-recognized named exports
 * like `metadata`/`generateStaticParams`) — `next build`'s generated route
 * types reject anything else, which is exactly what `export function
 * openModelsTab` from page.tsx broke. Putting it here also avoids a
 * page.tsx <-> stage-component circular import (the same reason `FreeBox`/
 * `EmptyStage` import this instead of a page.tsx export today).
 */

import { requestSectionOpen } from "@/components/shared/Section";

export type ModelsTab = "providers" | "local" | "download";

export const MODELS_TAB_EVENT = "mc:models-tab";

/** Jump to the Models section and select a tab (used by the LM Studio
 *  pointer, and by FreeBox/EmptyStage's "add model" actions). */
export function openModelsTab(tab: ModelsTab) {
  requestSectionOpen("models");
  window.dispatchEvent(new CustomEvent(MODELS_TAB_EVENT, { detail: tab }));
  requestAnimationFrame(() => {
    document.getElementById("models")?.scrollIntoView({ behavior: "smooth", block: "start" });
  });
}
