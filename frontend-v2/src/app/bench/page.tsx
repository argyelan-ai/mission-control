"use client";

// Thin app-route wrapper — the actual UI lives in the strippable vertical
// (ADR-044 §4). A stripped release removes this route file together with
// src/verticals/bench_studio/ and flips VERTICALS.benchStudio to false.
import BenchStudioPage from "@/verticals/bench_studio/BenchStudioPage";

export default function Page() {
  return <BenchStudioPage />;
}
