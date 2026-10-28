/**
 * Mount fixture for `bench-result-shots.mjs`: the REAL bench result page
 * (`ResultView`) with the real query client, in the requested language. The
 * API answers come from the driver (Playwright routes serve the backend's
 * contract fixtures), so the page shows exactly what a record says.
 * Not a test: it exists for the before/after pictures of a visible change.
 */
import React from "react";
import { createRoot } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ResultView } from "@/verticals/bench_studio/w1/result/ResultPage";
import en from "../messages/en.json";
import de from "../messages/de.json";

declare global {
  interface Window {
    __mountBenchResult?: (opts: { locale: "en" | "de"; testId: string }) => void;
  }
}

function App({ locale, testId }: { locale: "en" | "de"; testId: string }) {
  const [qc] = React.useState(
    () => new QueryClient({ defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } } }),
  );
  return (
    <NextIntlClientProvider locale={locale} messages={(locale === "de" ? de : en) as never} timeZone="UTC">
      <QueryClientProvider client={qc}>
        <main className="min-h-[100dvh] p-4 lg:p-8" style={{ background: "var(--color-bg-deep)" }}>
          <ResultView testId={testId} />
        </main>
      </QueryClientProvider>
    </NextIntlClientProvider>
  );
}

export function mount() {
  localStorage.setItem("mc_auth_token", "shot-token");
  const root = createRoot(document.getElementById("root")!);
  window.__mountBenchResult = (opts) => root.render(<App key={JSON.stringify(opts)} {...opts} />);
}
