/**
 * Server-safe locale constants + the inline <head> script (no React import) —
 * used by app/layout.tsx (a server component) and by lib/locale.ts.
 *
 * Why a script before first paint: the server picks the UI language from the
 * NEXT_LOCALE cookie only (it cannot see the login token, which lives in
 * localStorage). Without the cookie it renders the browser language, so a
 * user who chose German but lost the cookie saw English. This script runs
 * before anything is painted and
 *   - has the language cached from a choice → writes the cookie and, if the
 *     server rendered another language, reloads while the page is hidden
 *     (English is never shown);
 *   - has no cached choice but a login → hides the page and lets
 *     <LocaleSync> ask the server for the saved language (failsafe: shown
 *     again after LOCALE_PENDING_MS whatever happens).
 * Dependency-free on purpose; locale.test executes it.
 */
import { LOCALES, LOCALE_COOKIE } from "@/i18n/locales";
import { AUTH_TOKEN_KEY } from "./authToken";

/** Last language the user CHOSE (or that was saved for them) on this browser. */
export const LOCALE_CACHE_KEY = "mc_locale";
/** Set when the cookie was filled automatically from the browser language. */
export const LOCALE_AUTO_KEY = "mc_locale_auto";
/** sessionStorage: the language we already reloaded for (no reload loop). */
export const LOCALE_RELOAD_KEY = "mc_locale_reload";
/** sessionStorage: the server said "nothing saved" (404) in this session. */
export const LOCALE_NONE_KEY = "mc_locale_none";
/** On <html> while the saved language is still being looked up. */
export const LOCALE_PENDING_ATTR = "data-locale-pending";
export const LOCALE_PENDING_MS = 2500;
export const LOCALE_COOKIE_MAX_AGE = 31536000;

const locales = JSON.stringify(LOCALES);

export const LOCALE_INIT_SCRIPT = `(function(){try{var L=${locales},d=document,e=d.documentElement,ok=function(v){return L.indexOf(v)>=0};var m=d.cookie.match(/(?:^|;\\s*)${LOCALE_COOKIE}=([^;]*)/);if(m&&ok(m[1])){try{sessionStorage.removeItem("${LOCALE_RELOAD_KEY}")}catch(_){}return}var c=null,t=null;try{c=localStorage.getItem("${LOCALE_CACHE_KEY}");t=localStorage.getItem("${AUTH_TOKEN_KEY}")}catch(_){}if(ok(c)){d.cookie="${LOCALE_COOKIE}="+c+"; path=/; max-age=${LOCALE_COOKIE_MAX_AGE}; samesite=lax";if(c!==e.lang&&d.cookie.indexOf("${LOCALE_COOKIE}="+c)>=0){var r=null;try{r=sessionStorage.getItem("${LOCALE_RELOAD_KEY}")}catch(_){}if(r!==c){try{sessionStorage.setItem("${LOCALE_RELOAD_KEY}",c)}catch(_){}e.style.visibility="hidden";location.reload()}}return}if(t){e.setAttribute("${LOCALE_PENDING_ATTR}","");e.style.visibility="hidden";setTimeout(function(){if(e.hasAttribute("${LOCALE_PENDING_ATTR}")){e.removeAttribute("${LOCALE_PENDING_ATTR}");e.style.visibility=""}},${LOCALE_PENDING_MS})}}catch(_){}})();`;
