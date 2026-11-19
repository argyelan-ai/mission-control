"use client";

// Bench W1 test sheet (P3) — package B. Replaces NewChallengeDialog: there is
// no second test dialog.
//
// Mounted once by BenchStudioPage (frozen signature: no props). It reads the
// sheet request from the URL (lib/benchLinks: `/bench?test=1&runtime=…`),
// loads GET /bench/plan and shows every row pre-filled as chips. The live
// model is tested with the entry tap + Start: 2 taps, nothing typed.
// Start calls POST /bench/tests (a 409 CostConfirmation turns the button
// into the price question, D4: over the cap a second one) and lands on
// /bench/r/<id>. Closing removes the sheet params from the URL.
import { useCallback, useMemo } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { parseBenchTestParams } from "@/lib/benchLinks";
import { SheetFrame } from "./SheetFrame";
import { TestSheet } from "./TestSheet";

/** URL params that belong to the sheet — removed on close, the rest stays. */
const SHEET_PARAMS = ["test", "runtime", "preset", "template", "rerun", "from"];

export function TestSheetHost() {
  const t = useTranslations("benchSheet");
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const search = searchParams?.toString() ?? "";
  const params = useMemo(() => parseBenchTestParams(new URLSearchParams(search)), [search]);

  const close = useCallback(() => {
    const next = new URLSearchParams(search);
    for (const k of SHEET_PARAMS) next.delete(k);
    const q = next.toString();
    router.replace(q ? `${pathname}?${q}` : pathname, { scroll: false });
  }, [router, pathname, search]);

  // A new link (another runtime, another preset) starts a fresh sheet.
  const sheetKey = params
    ? [params.runtimeId, params.preset, params.templateId, params.rerunOf, params.trigger].join("|")
    : "closed";

  return (
    <SheetFrame open={params !== null} onClose={close} label={t("title")}>
      {params && <TestSheet key={sheetKey} params={params} onClose={close} />}
    </SheetFrame>
  );
}
