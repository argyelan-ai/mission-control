/**
 * The phone action bar's main button mirrors the task detail's ONE primary
 * button (K11) instead of re-implementing it: approve, answer, reply, open
 * log, requeue … all keep their single implementation in the next-step card.
 *
 * Every primary button of the next step carries the `main-action` class
 * (PRIMARY_BTN in components/task/detail/nextStepStyle.ts). The first one in
 * document order is the next step.
 */

export const MAIN_ACTION_CLASS = "main-action";

export type FoundMainAction = {
  el: HTMLButtonElement;
  label: string;
  /** "reply" when the primary IS the reply button — the bar then shows no separate Reply. */
  kind: string | null;
};

export function findMainAction(root: ParentNode | null | undefined): FoundMainAction | null {
  const el = root?.querySelector<HTMLButtonElement>(`button.${MAIN_ACTION_CLASS}`) ?? null;
  if (!el) return null;
  const label = (el.textContent ?? "").replace(/\s+/g, " ").trim();
  if (!label) return null;
  return { el, label, kind: el.getAttribute("data-main-kind") };
}

/**
 * Tap on the bar's main button. An enabled primary runs as if tapped in
 * place. A disabled one still waits for input (approve needs a reason): the
 * bar brings that input into view and focuses it, so the next tap decides.
 */
export function runMainAction(el: HTMLButtonElement): "clicked" | "focused" {
  if (!el.disabled) {
    el.click();
    return "clicked";
  }
  const scope = el.closest("section") ?? el.parentElement ?? el;
  const input = scope.querySelector<HTMLElement>("textarea:not([disabled]), input:not([disabled])");
  const target = input ?? el;
  target.scrollIntoView?.({ block: "center", behavior: "smooth" });
  target.focus?.({ preventScroll: true });
  return "focused";
}
