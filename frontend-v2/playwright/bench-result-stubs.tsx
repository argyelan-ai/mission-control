/**
 * Stand-ins for `next/link` and `next/navigation` in the bench picture gate
 * (`bench-result-shots.mjs`). The real components are mounted outside Next,
 * so routing is a no-op and links are plain anchors.
 */
import React from "react";

export default function Link({ href, children, ...rest }: { href: string; children?: React.ReactNode } & Record<string, unknown>) {
  return (
    <a href={href} {...(rest as React.AnchorHTMLAttributes<HTMLAnchorElement>)}>
      {children}
    </a>
  );
}

const router = { push: () => {}, replace: () => {}, back: () => {}, prefetch: () => {}, refresh: () => {} };

export function useRouter() {
  return router;
}

export function usePathname() {
  return "/bench/r/shot";
}

export function useSearchParams() {
  return new URLSearchParams();
}

export function redirect() {}
export function notFound() {}
