"use client";

import dynamic from "next/dynamic";
import { C } from "@/lib/colors";

const OfficeView = dynamic(() => import("@/components/pages/OfficeView"), {
  ssr: false,
  loading: () => (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        height: "100dvh", // 100vh ist auf iOS zu hoch (MOBILE-SPEC M3)
        background: C.bgBase,
        color: C.textMuted,
        fontSize: 14,
      }}
    >
      Loading 3D Office…
    </div>
  ),
});

export default function OfficePage() {
  return <OfficeView />;
}
