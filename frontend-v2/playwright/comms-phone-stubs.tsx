/**
 * Stand-ins for the picture gate of the ADR-100 phone view
 * (`comms-phone-shots.mjs`): `next/link` and `next/navigation`. The real
 * pages and components are mounted
 * outside Next, so links are plain anchors and the query string is read
 * from `window.location`.
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
  return "/inbox";
}
export function useSearchParams() {
  return new URLSearchParams(window.location.search);
}
export function redirect() {}
export function notFound() {}
