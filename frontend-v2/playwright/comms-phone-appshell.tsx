/**
 * AppShell stand-in for `comms-phone-shots.mjs`: the page padding the real
 * shell gives a page (px-4 on a phone), without its bars, login and polls.
 */
import React from "react";

export default function AppShell({ children }: { children: React.ReactNode }) {
  return <main className="px-4 md:px-6 lg:px-8 pt-6 pb-16">{children}</main>;
}
