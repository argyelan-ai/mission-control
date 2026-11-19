"use client";

import { useEffect } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { ChevronLeft } from "lucide-react";
import AppShell from "@/components/layout/AppShell";
import { C } from "@/lib/colors";
import { BENCH_LIST_HREF, benchResultHref } from "@/lib/benchLinks";
import { PromptLibraryTab } from "./PromptLibraryTab";
import { TestList } from "./w1/list/TestList";
import { TestSheetHost } from "./w1/sheet/TestSheetHost";

/**
 * /bench — bench W1 (package D).
 *
 *   /bench                     the test list (P7)
 *   /bench?view=library        the prompt library (P8, kept)
 *   /bench?test=1&…            either of the above + the test sheet (B)
 *   /bench?challenge=<id>      old W0 link → /bench/r/<id> (a challenge id is the test id)
 */
export default function BenchStudioPage() {
  const t = useTranslations("benchHome");
  const router = useRouter();
  const searchParams = useSearchParams();
  const legacyChallenge = searchParams.get("challenge");
  const view = searchParams.get("view") === "library" ? "library" : "list";

  useEffect(() => {
    if (legacyChallenge) router.replace(benchResultHref(legacyChallenge));
  }, [legacyChallenge, router]);

  if (legacyChallenge) return null;

  return (
    // The list lays out by its own width (rows flow into columns), so it
    // takes the whole page; the library keeps the app's 1600 px column.
    <AppShell wide={view === "list"}>
      {view === "library" ? (
        <div className="flex flex-col gap-6">
          <div className="flex flex-col gap-1">
            {/* Context bar (K12): back to the list. */}
            <Link
              href={BENCH_LIST_HREF}
              className="self-start inline-flex items-center gap-1 h-11 -ml-2 pr-3 rounded-md text-sm hover:bg-[var(--color-bg-hover)]"
              style={{ color: C.textSecondary }}
            >
              <ChevronLeft size={18} aria-hidden />
              {t("libraryBack")}
            </Link>
            <h1 className="display text-2xl font-semibold" style={{ color: C.textPrimary }}>
              {t("library")}
            </h1>
            <p className="text-sm max-w-prose" style={{ color: C.textSecondary }}>
              {t("libraryLead")}
            </p>
          </div>
          <PromptLibraryTab />
        </div>
      ) : (
        <TestList />
      )}
      {/* Bench W1 test sheet (package B) — opens from `/bench?test=1&…`
          (lib/benchLinks), over the list or the library. */}
      <TestSheetHost />
    </AppShell>
  );
}
