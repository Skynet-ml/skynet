import fs from "node:fs";
import path from "node:path";
import { lastLine, type DockerCtx } from "./postgres";
import { runCommand } from "./supervisor";

export const SANDBOX_REPO = "skynet-local-sandbox";

async function imageId(ctx: DockerCtx, ref: string): Promise<string | null> {
  const res = await runCommand(ctx.docker, ["image", "inspect", "--format", "{{.Id}}", ref], {
    env: ctx.env,
    timeoutMs: 30_000,
  });
  const id = res.stdout.trim();
  return res.code === 0 && id.startsWith("sha256:") ? id : null;
}

/** A Docker tag from a bundle id: allowed characters only, at most 128. */
export function sandboxTag(bundleId: string): string {
  const tag = bundleId.replace(/[^A-Za-z0-9_.-]/g, "-").replace(/^[.-]+/, "").slice(0, 128);
  return tag || "local";
}

/**
 * Make sure `skynet-local-sandbox:<bundle id>` exists and return its image ID.
 * The bundle id changes with every packaged build (version, commit, build
 * time), the same key stageRuntime uses for the backend copy, so a new build
 * never runs jobs in an image made from an older backend Dockerfile.
 * The backend runs sandboxes by ID (`APP_LOCAL_SANDBOX_IMAGE=sha256:...`) so a
 * retag can never swap the image under a running job.
 */
export async function ensureSandboxImage(
  ctx: DockerCtx,
  backendDir: string,
  bundleId: string,
  logFile: string,
): Promise<string> {
  const ref = `${SANDBOX_REPO}:${sandboxTag(bundleId)}`;
  const existing = await imageId(ctx, ref);
  if (existing) return existing;
  const log = fs.createWriteStream(logFile, { flags: "a" });
  try {
    // backend/Dockerfile installs x64-only harness binaries (it is the Railway image),
    // so Apple silicon builds it for linux/amd64 and Docker Desktop runs it under Rosetta.
    const build = await runCommand(ctx.docker, ["build", "--platform", "linux/amd64", "-t", ref, backendDir], {
      env: ctx.env,
      cwd: path.dirname(backendDir),
      timeoutMs: 60 * 60_000,
      log,
    });
    if (build.code !== 0) {
      // BuildKit ends with a "View build details" link; the ERROR line says what broke.
      const errorLine = build.stderr.split("\n").filter((l) => l.includes("ERROR")).pop();
      throw new Error(`docker build ${ref} failed: ${errorLine?.trim() || lastLine(build.stderr)}`);
    }
  } finally {
    log.end();
  }
  const built = await imageId(ctx, ref);
  if (!built) throw new Error(`docker build ${ref} produced no image`);
  await removeOlderTags(ctx, ref);
  return built;
}

/** Drop the tags of earlier builds. Docker refuses to remove an image a container still uses. */
async function removeOlderTags(ctx: DockerCtx, keep: string): Promise<void> {
  const list = await runCommand(ctx.docker, ["image", "ls", SANDBOX_REPO, "--format", "{{.Repository}}:{{.Tag}}"], {
    env: ctx.env,
    timeoutMs: 30_000,
  });
  const old = list.stdout.split("\n").map((l) => l.trim()).filter((l) => l.startsWith(`${SANDBOX_REPO}:`) && !l.endsWith(":<none>") && l !== keep);
  if (old.length) await runCommand(ctx.docker, ["image", "rm", ...old], { env: ctx.env, timeoutMs: 120_000 });
}
