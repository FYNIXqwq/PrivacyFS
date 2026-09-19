/** Optional entry for an explicitly prepared Pi package environment.
 * No execution on import and no implicit provider/auth/model selection.
 * This file needs @earendil-works/pi-agent-core 0.84.4; no automatic install.
 */
import { Agent } from "@earendil-works/pi-agent-core";
import { runPiBatch, harnessVersion } from "./attack-harness.mjs";

export async function evaluateWithPi(batch, { model, streamFn, allowModelInvocation = false }) {
  if (!allowModelInvocation || !model?.provider || !model?.id || typeof streamFn !== "function")
    throw new Error("EXPLICIT_MODEL_CONFIGURATION_REQUIRED");
  return runPiBatch(batch, { createAgent: options => new Agent(options), model, streamFn }, {
    implementation: "pi-agent-core-0.84.4", version: harnessVersion,
    provider: model.provider, model: model.id, mode: "live"
  });
}
