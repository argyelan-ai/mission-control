import { readFileSync } from "fs";
import path from "path";

/** Test helper: read custom-property declarations from globals.css blocks. */
export const GLOBALS_CSS = path.resolve(__dirname, "../../styles/globals.css");

export function cssText(): string {
  return readFileSync(GLOBALS_CSS, "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
}

/** Declarations of the first top-level block whose selector matches exactly. */
export function blockVars(selector: string, css = cssText()): Map<string, string> {
  const esc = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const re = new RegExp(`(^|\\n)\\s*${esc}\\s*\\{`, "g");
  const m = re.exec(css);
  const out = new Map<string, string>();
  if (!m) return out;
  let i = m.index + m[0].length;
  let depth = 1;
  const start = i;
  while (i < css.length && depth > 0) {
    if (css[i] === "{") depth++;
    else if (css[i] === "}") depth--;
    i++;
  }
  const body = css.slice(start, i - 1);
  for (const d of body.matchAll(/(--[a-z0-9-]+)\s*:\s*([^;]+);/gi)) out.set(d[1], d[2].trim());
  return out;
}

/** Follow var() references inside one mode's variable map down to a literal. */
export function resolveVar(value: string, vars: Map<string, string>, depth = 0): string {
  if (depth > 10) throw new Error(`var() cycle at ${value}`);
  return value.replace(/var\((--[a-z0-9-]+)\)/gi, (_m, name: string) => {
    const v = vars.get(name);
    if (v === undefined) throw new Error(`undefined ${name}`);
    return resolveVar(v, vars, depth + 1);
  });
}
