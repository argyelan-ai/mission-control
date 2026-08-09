"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

/** Shared with ActiveDownloads via the same query key — one request serves both. */
export function useDownloadCount(): number {
  const { data } = useQuery({
    queryKey: ["lms-downloads"],
    queryFn: () => api.lmstudio.downloads(),
    refetchInterval: 4_000,
  });
  return data?.downloads.length ?? 0;
}
