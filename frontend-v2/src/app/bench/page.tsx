"use client";

// Thin app-route wrapper — the actual UI lives in the strippable vertical
// (ADR-044 §4). A stripped release removes this route file together with
// src/verticals/bench_studio/ and flips VERTICALS.benchStudio to false.
import { Suspense } from "react";
import BenchStudioPage from "@/verticals/bench_studio/BenchStudioPage";

// useSearchParams (the open challenge in `?challenge=`) needs a Suspense
// boundary in the app router.
export default function Page() {
  return (
    <Suspense fallback={null}>
      <BenchStudioPage />
    </Suspense>
  );
}
