"use client";

// Thin app-route wrapper for the bench W1 result page (P5) — the UI lives in
// the strippable vertical (ADR-044 §4); a stripped release removes this
// route together with src/verticals/bench_studio/.
import { useParams } from "next/navigation";
import ResultPage from "@/verticals/bench_studio/w1/result/ResultPage";

export default function Page() {
  const params = useParams<{ id: string }>();
  return <ResultPage testId={params?.id ?? ""} />;
}
