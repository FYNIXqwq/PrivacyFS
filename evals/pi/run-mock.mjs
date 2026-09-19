/** Offline protocol smoke only; this fake is NOT a real Pi/model integration. */
import { readFile, writeFile } from "node:fs/promises";
import { runPiBatch, harnessVersion } from "./attack-harness.mjs";

class MockAgent {
  state = { messages: [] };
  async prompt() {
    this.state.messages = [{ role: "assistant", stopReason: "stop",
      content: [{ type: "text", text: '{"identities":[],"attributes":[]}' }] }];
  }
  abort() {}
}

try {
  const [input, output] = process.argv.slice(2);
  if (!input || !output || process.argv.length !== 4) throw new Error("INVALID_ARGUMENTS");
  const batch = JSON.parse(await readFile(input, "utf8"));
  const result = await runPiBatch(batch, { createAgent: options => new MockAgent(options), model: {}, streamFn: () => {
    throw new Error("MOCK_HAS_NO_PROVIDER");
  } }, { implementation: "pi-api-contract-fake", version: harnessVersion,
    provider: "none", model: "abstain-fake", mode: "mock" });
  await writeFile(output, JSON.stringify(result, null, 2) + "\n", { encoding: "utf8", flag: "wx" });
  process.stdout.write(JSON.stringify({ status: "complete", mode: "mock", responses: result.responses.length }) + "\n");
} catch {
  process.stderr.write('{"status":"failed","code":"MOCK_PROTOCOL_ERROR"}\n');
  process.exitCode = 2;
}
