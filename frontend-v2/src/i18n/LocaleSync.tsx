"use client";

import { useEffect } from "react";
import { syncLocaleOnLoad } from "@/lib/locale";

/**
 * Restores the user's UI language when the NEXT_LOCALE cookie is missing and
 * writes the cookie again (lib/locale.ts). Mounted once in the root layout;
 * renders nothing. The <head> script (lib/localeScript.ts) keeps the page
 * hidden meanwhile, so the wrong language is never painted.
 */
export function LocaleSync() {
  useEffect(() => {
    void syncLocaleOnLoad();
  }, []);
  return null;
}
