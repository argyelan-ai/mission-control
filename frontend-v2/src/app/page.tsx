import { redirect } from "next/navigation";

/**
 * MC opens in the last chat (ADR-102 principle 1, operator decision
 * 2026-11-18). The chat page itself knows which chat that was — it restores
 * the last agent or group this device opened (`mc-sessions-last-agent`,
 * `mc-sessions-last-group`) and shows it straight away. The dashboard that
 * used to live here moved to /home (Settings › More).
 */
const START_PATH = "/sessions";

export default function Page() {
  redirect(START_PATH);
}
