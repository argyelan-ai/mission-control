// Stop and start one service of the throw-away journey stack — for sabotage
// steps ("the model is gone"). Containers are found by their compose labels,
// like sql() in mc.ts, so no compose file is parsed and only containers of
// the journey project can be touched.
import { execFileSync } from "node:child_process";
import { PROJECT } from "./mc";

function containerId(service: string): string {
  const ids = execFileSync(
    "docker",
    [
      "ps", "-aq",
      "--filter", `label=com.docker.compose.project=${PROJECT}`,
      "--filter", `label=com.docker.compose.service=${service}`,
    ],
    { encoding: "utf8" },
  )
    .trim()
    .split("\n")
    .filter(Boolean);
  if (ids.length !== 1) throw new Error(`expected one ${PROJECT} ${service} container, found ${ids.length}`);
  return ids[0];
}

export function stopService(service: string): void {
  execFileSync("docker", ["stop", "-t", "2", containerId(service)], { encoding: "utf8" });
}

/** Starts the service again and waits until its healthcheck says healthy. */
export async function startService(service: string, timeoutMs = 60_000): Promise<void> {
  const id = containerId(service);
  execFileSync("docker", ["start", id], { encoding: "utf8" });
  const until = Date.now() + timeoutMs;
  while (Date.now() < until) {
    const health = execFileSync("docker", ["inspect", "-f", "{{.State.Health.Status}}", id], {
      encoding: "utf8",
    }).trim();
    if (health === "healthy") return;
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error(`${service} did not become healthy within ${timeoutMs} ms`);
}

/** Runs `fn` with the service stopped and always starts it again. */
export async function withServiceDown<T>(service: string, fn: () => Promise<T>): Promise<T> {
  stopService(service);
  try {
    return await fn();
  } finally {
    await startService(service);
  }
}
