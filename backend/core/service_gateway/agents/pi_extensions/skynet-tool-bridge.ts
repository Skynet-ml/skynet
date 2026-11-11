/**
 * Pi extension that gives the model the tools of one Skynet agent turn (SKY-327).
 *
 * The backend owns every tool. This extension only forwards: it reads the
 * turn's tool specs from the bridge's GET /tools and sends each call to
 * POST /call. Approvals, wizard writes and stream events stay in the backend.
 * The bridge URL and its per-turn token come from the environment.
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

type ToolSpec = { name: string; description: string; parameters: Record<string, unknown> };
type CallReply = { ok: boolean; text?: string; error?: string; terminate?: boolean };

export default async function (pi: ExtensionAPI) {
  const base = process.env.SKYNET_BRIDGE_URL;
  const token = process.env.SKYNET_BRIDGE_TOKEN;
  if (!base || !token) throw new Error("SKYNET_BRIDGE_URL or SKYNET_BRIDGE_TOKEN is not set");
  const headers = { authorization: `Bearer ${token}`, "content-type": "application/json" };
  const listing = await fetch(`${base}/tools`, { headers });
  if (!listing.ok) throw new Error(`Skynet bridge refused the tool listing: ${listing.status}`);
  const specs = (await listing.json()) as ToolSpec[];
  for (const spec of specs) {
    pi.registerTool({
      name: spec.name,
      label: spec.name,
      description: spec.description,
      parameters: Type.Unsafe(spec.parameters),
      // The backend runs one call at a time, as the old loop did.
      executionMode: "sequential",
      async execute(_toolCallId, params) {
        const response = await fetch(`${base}/call`, {
          method: "POST",
          headers,
          body: JSON.stringify({ tool: spec.name, args: params ?? {} }),
        });
        // The bridge sends whitespace while an approval waits, so the body
        // timeout never fires; JSON.parse skips that leading whitespace.
        const reply = JSON.parse(await response.text()) as CallReply;
        if (!reply.ok) throw new Error(reply.error ?? `${spec.name} failed`);
        return {
          content: [{ type: "text", text: reply.text ?? "" }],
          details: undefined,
          terminate: reply.terminate === true,
        };
      },
    });
  }
}
