import test from "node:test";
import assert from "node:assert/strict";
import { runPiAttack, runPiBatch, protocolVersion, requestDigest } from "./attack-harness.mjs";

const request = { schema_version: protocolVersion, request_id: "rel-00/raw", request_sha256: "a".repeat(64),
  view: { tables: [], background: [], history: [] } };
request.request_sha256 = requestDigest(request);
const metadata = { implementation: "fake", version: "1", provider: "none", model: "none", mode: "mock" };

function options(text = '{"identities":[],"attributes":[]}') {
  return { model: {}, streamFn() { throw new Error("NO_NETWORK"); }, createAgent(config) {
    assert.deepEqual(config.initialState.tools, []);
    assert.deepEqual(config.initialState.messages, []);
    assert.equal(config.beforeToolCall().block, true);
    return { state: { messages: [] }, async prompt(value) {
      assert.deepEqual(JSON.parse(value), request.view);
      assert.equal(value.includes("gold"), false);
      this.state.messages.push({ role: "assistant", stopReason: "stop", content: [{ type: "text", text }] });
    }, abort() {} };
  } };
}

test("only the public view enters the agent; empty valid output is an abstention", async () => {
  assert.equal((await runPiAttack(request, options())).status, "complete");
});
test("malformed output and protocol-invalid claims are not successful abstentions", async () => {
  for (const text of ["not JSON", '{"identities":[["x"]],"attributes":[]}', '{"identities":[],"attributes":[],"gold":[]}']) {
    assert.equal((await runPiAttack(request, options(text))).status, "invalid");
  }
});
test("failed stop reason discards even plausible text", async () => {
  const config = options();
  config.createAgent = () => ({ state: { messages: [{ role: "assistant", stopReason: "error",
    content: [{ type: "text", text: '{"identities":[],"attributes":[]}' }] }] }, async prompt() {}, abort() {} });
  assert.equal((await runPiAttack(request, config)).status, "failed");
});
test("timeout aborts and stops batch before another request", async () => {
  let created = 0, aborted = 0;
  const config = { model: {}, streamFn() {}, timeoutMs: 5, createAgent() {
    created++;
    return { state: { messages: [] }, prompt: () => new Promise(() => {}), abort() { aborted++; } };
  } };
  const second = { ...request, request_id: "rel-01/raw" };
  second.request_sha256 = requestDigest(second);
  const batch = await runPiBatch({ schema_version: protocolVersion,
    requests: [request, second] }, config, metadata);
  assert.equal(batch.responses[0].status, "timeout");
  assert.equal(created, 1);
  assert.equal(aborted, 1);
});
test("batch validation rejects gold injection and duplicates before invoking any agent", async () => {
  let created = false;
  const config = { createAgent() { created = true; throw new Error("UNEXPECTED"); } };
  await assert.rejects(runPiBatch({ schema_version: protocolVersion, requests: [request, { ...request, gold: [] }] }, config, metadata));
  await assert.rejects(runPiBatch({ schema_version: protocolVersion, requests: [request, request] }, config, metadata));
  assert.equal(created, false);
});
test("edited public content cannot retain an old request digest", async () => {
  const edited = { ...request, view: { ...request.view, history: [{ identity: "extra", key: "", topic: "" }] } };
  await assert.rejects(runPiAttack(edited, options()), /REQUEST_DIGEST_MISMATCH/);
});
