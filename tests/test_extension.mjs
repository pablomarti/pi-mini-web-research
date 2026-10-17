// Run with PI_CODING_AGENT_DIR pointing at an installed pi-coding-agent package.
// Uses pi's real loader, tool wrapper and subprocess executor; no model calls.
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const scratch = mkdtempSync(path.join(os.tmpdir(), "pi-search-test-"));
const savedEnv = { ...process.env };
process.env.HOME = scratch;
for (const key of Object.keys(process.env)) {
    if (/API_KEY|TOKEN|SECRET/.test(key)) delete process.env[key];
}
process.env.PYTHONPATH = path.join(root, "tests", "mcp_fixture");
process.env.PI_TEST_REQUESTS = path.join(scratch, "requests.jsonl");
const piDir = process.env.PI_CODING_AGENT_DIR;
assert.ok(piDir, "Set PI_CODING_AGENT_DIR to an installed pi-coding-agent package");
const { loadExtensions } = await import(pathToFileURL(path.join(piDir, "dist/core/extensions/loader.js")));
const { wrapRegisteredTool, wrapToolWithExtensions } = await import(pathToFileURL(path.join(piDir, "dist/core/extensions/wrapper.js")));
const { SessionManager } = await import(pathToFileURL(path.join(piDir, "dist/core/session-manager.js")));
const requests = () => {
    try { return readFileSync(process.env.PI_TEST_REQUESTS, "utf8").trim().split("\n").filter(Boolean).map(JSON.parse); }
    catch { return []; }
};
try {
    const loaded = await loadExtensions([path.join(root, "extensions/web-research/index.ts")], root);
    assert.deepEqual(loaded.errors, []);
    const extension = loaded.extensions[0];
    const registered = extension.tools.get("web_search");
    const sessionManager = SessionManager.inMemory(root);
    const tool = wrapRegisteredTool(registered, { createContext: () => ({ sessionManager }) });
    const run = signal => tool.execute("scripted-tool-call", { query: "Python documentation" }, signal);

    delete process.env.PI_WEB_RESEARCH_SEARCH_PROVIDER;
    assert.equal((await run()).details.provider, "duckduckgo");
    assert.equal(requests().at(-1).url, "https://lite.duckduckgo.com/lite");
    process.env.PI_WEB_RESEARCH_SEARCH_PROVIDER = "duckduckgo";
    assert.match((await run()).content[0].text, /DDG result/);

    process.env.PI_WEB_RESEARCH_SEARCH_PROVIDER = "parallel";
    for (const mode of ["json", "sse", "text"]) {
        process.env.PI_TEST_MODE = mode;
        const result = await run();
        assert.equal(result.details.provider, "parallel");
        assert.equal(result.details.resultCount, 1);
        assert.match(result.content[0].text, /Reference\n.*https:\/\/example.com\/reference\?q=a%26b\n.*Useful evidence\nSecond excerpt/);
    }
    const mcpRequests = requests().filter(r => r.payload);
    for (const request of mcpRequests) {
        assert.equal(request.url, "https://search.parallel.ai/mcp");
        assert.match(request.headers["User-agent"], /^pi-mini-web-research\/1\.0\.0/);
        assert.equal(request.headers.Authorization, undefined);
        assert.ok(request.timeout > 0 && request.timeout <= 20);
        if (request.payload.method !== "initialize") {
            assert.equal(request.headers["Mcp-session-id"], "fixture-mcp-session");
            assert.equal(request.headers["Mcp-protocol-version"], "2025-03-26");
        }
    }
    const calls = () => requests().filter(r => r.payload?.method === "tools/call");
    const firstSession = calls()[0].payload.params.arguments.session_id;
    assert.equal(new Set(calls().map(r => r.payload.params.arguments.session_id)).size, 1);
    assert.deepEqual(calls()[0].payload.params.arguments.search_queries, ["Python documentation"]);
    sessionManager.newSession();
    await run();
    assert.notEqual(calls().at(-1).payload.params.arguments.session_id, firstSession);

    for (const [mode, pattern] of [["error", /fixture provider failure/], ["timeout", /timed out/], ["malformed", /Search error/]]) {
        process.env.PI_TEST_MODE = mode;
        const before = requests().length;
        await assert.rejects(run(), pattern);
        assert.equal(requests().length - before, 3, "No fallback after provider failure");
    }
    for (const provider of ["none", "misspelled", ""]) {
        process.env.PI_WEB_RESEARCH_SEARCH_PROVIDER = provider;
        const before = requests().length;
        await assert.rejects(run(), provider === "none" ? /disabled/ : /Unknown/);
        assert.equal(requests().length, before);
    }
    process.env.PI_WEB_RESEARCH_SEARCH_PROVIDER = "parallel";
    const blocked = wrapToolWithExtensions(tool, {
        hasHandlers: event => event === "tool_call",
        emitToolCall: async () => ({ block: true, reason: "Permission denied" }),
    });
    const before = requests().length;
    await assert.rejects(blocked.execute("denied", { query: "test" }), /Permission denied/);
    assert.equal(requests().length, before);

    process.env.PI_TEST_MODE = "stall";
    const controller = new AbortController();
    const pending = run(controller.signal);
    setTimeout(() => controller.abort(), 200);
    await assert.rejects(pending, /Search cancelled/);
    const preAborted = new AbortController();
    preAborted.abort();
    await assert.rejects(run(preAborted.signal), /Search cancelled/);
    if (process.env.PI_TEST_PROCESS_TIMEOUT === "1") {
        await assert.rejects(run(), /Search timed out/);
    }
    console.log("PASS: real pi loader, registered tool, Python subprocess, JSON/SSE/text, resolver, sessions, headers, permissions, errors, timeout and cancellation");
} finally {
    process.env = savedEnv;
    rmSync(scratch, { recursive: true, force: true });
}
