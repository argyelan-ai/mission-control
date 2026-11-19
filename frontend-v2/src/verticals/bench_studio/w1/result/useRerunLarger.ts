"use client";

// "Re-run with a larger budget" from the private truncation warning — one
// hook for the result page and the X-Kit sheet. A paid model answers
// 409 CostConfirmation (D4): the same button turns into the price question
// ("Start · $X — OK?", over the cap a second one) and the next tap repeats
// the re-run WITH the confirmation, so the larger budget is never lost on a
// detour through the test sheet.
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { notify } from "@/lib/notify";
import { benchW1Api, benchW1Keys, parseCostConfirmation } from "../api";
import type { BenchTestDetail, CostConfirmation } from "../types";
import { apiErrorText } from "./errors";

export function useRerunLarger(
  testId: string,
  onStarted: (next: BenchTestDetail) => void,
  failedText: string,
) {
  const qc = useQueryClient();
  const [costAsk, setCostAsk] = useState<CostConfirmation | null>(null);
  const mutation = useMutation({
    mutationFn: (ask: CostConfirmation | null) =>
      ask
        ? benchW1Api.tests.rerun(
            testId,
            "larger",
            ask.price_unknown || ask.usd === null
              ? { confirm_unknown_cost: true }
              : { cost_confirmed_usd: ask.usd, confirm_over_cap: ask.step === 2 },
          )
        : benchW1Api.tests.rerun(testId, "larger"),
    onSuccess: (next) => {
      setCostAsk(null);
      void qc.invalidateQueries({ queryKey: benchW1Keys.testsAll() });
      onStarted(next);
    },
    onError: (err) => {
      const ask = parseCostConfirmation(err);
      if (ask) {
        setCostAsk(ask);
        return;
      }
      setCostAsk(null);
      notify.error(apiErrorText(err, failedText));
    },
  });
  return {
    costAsk,
    pending: mutation.isPending,
    run: () => mutation.mutate(costAsk),
  };
}
