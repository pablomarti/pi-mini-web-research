/**
 * pi-mini-web-research — Web research tools for pi
 *
 * Registers three tools:
 *   web_search  — Search the web via DuckDuckGo Lite
 *   web_fetch   — Fetch & clean a web page (HTML → plain text)
 *   github      — Search repos/code/issues, read files, list trees via gh CLI
 *
 * No npm dependencies. Uses Python3 stdlib for web operations and gh CLI for GitHub.
 */

import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { StringEnum } from "@mariozechner/pi-ai";
import { Type } from "@sinclair/typebox";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const EXTENSION_DIR = path.dirname(fileURLToPath(import.meta.url));
const PYTHON_SCRIPT = path.join(EXTENSION_DIR, "research.py");

const CACHE_DIR = path.join(os.homedir(), ".pi", "agent");
const CACHE_FILE = path.join(CACHE_DIR, "web-research-cache.json");
const CACHE_LOCK_FILE = path.join(CACHE_DIR, "web-research-cache.lock");
const CACHE_TTL_MS = 24 * 60 * 60 * 1000; // 24 hours
const MAX_CACHE_ENTRIES = 200;
const CACHE_LOCK_TIMEOUT_MS = 2_000;
const CACHE_LOCK_RETRY_MS = 25;

const DEFAULT_CHUNK = 12_000; // chars sent to LLM per fetch call
const MAX_CHUNK = 20_000;
const MAX_OFFSET = 5_000_000;
const MAX_TREE_ENTRIES = 500;

// ---------------------------------------------------------------------------
// Cache
// ---------------------------------------------------------------------------

interface CacheEntry {
	url: string;
	title: string;
	content: string;
	size: number;
	timestamp: number;
	contentType?: string;
}

interface CacheStore {
	[url: string]: CacheEntry;
}

function loadCache(): CacheStore {
	try {
		return JSON.parse(fs.readFileSync(CACHE_FILE, "utf-8"));
	} catch {
		return {};
	}
}

function saveCache(cache: CacheStore): void {
	// Prune expired
	const now = Date.now();
	const urls = Object.keys(cache);
	for (const url of urls) {
		if (now - cache[url].timestamp > CACHE_TTL_MS) delete cache[url];
	}

	// Prune oldest if over limit
	const remaining = Object.entries(cache);
	if (remaining.length > MAX_CACHE_ENTRIES) {
		remaining.sort((a, b) => a[1].timestamp - b[1].timestamp);
		const toRemove = remaining.length - MAX_CACHE_ENTRIES;
		for (let i = 0; i < toRemove; i++) delete cache[remaining[i][0]];
	}

	fs.mkdirSync(CACHE_DIR, { recursive: true });
	const tmp = CACHE_FILE + ".tmp";
	fs.writeFileSync(tmp, JSON.stringify(cache));
	fs.renameSync(tmp, CACHE_FILE);
}

function getCached(url: string): CacheEntry | null {
	const cache = loadCache();
	const entry = cache[url];
	if (entry && Date.now() - entry.timestamp < CACHE_TTL_MS) return entry;
	return null;
}

function putCache(entry: CacheEntry): void {
	const cache = loadCache();
	cache[entry.url] = entry;
	saveCache(cache);
}

function sleep(ms: number): Promise<void> {
	return new Promise((resolve) => setTimeout(resolve, ms));
}

async function withCacheLock<T>(fn: () => T): Promise<T> {
	fs.mkdirSync(CACHE_DIR, { recursive: true });
	const deadline = Date.now() + CACHE_LOCK_TIMEOUT_MS;

	while (true) {
		let handle: fs.promises.FileHandle | undefined;
		try {
			handle = await fs.promises.open(CACHE_LOCK_FILE, "wx");
			try {
				return fn();
			} finally {
				await handle.close();
				await fs.promises.unlink(CACHE_LOCK_FILE).catch(() => undefined);
			}
		} catch (error) {
			const errno = error as NodeJS.ErrnoException;
			if (errno.code !== "EEXIST") throw error;
			if (Date.now() > deadline) {
				throw new Error("Timed out waiting for cache lock");
			}
			await sleep(CACHE_LOCK_RETRY_MS);
		}
	}
}

async function putCacheAsync(entry: CacheEntry): Promise<void> {
	await withCacheLock(() => {
		putCache(entry);
	});
}

// ---------------------------------------------------------------------------
// Extension
// ---------------------------------------------------------------------------

export default function (pi: ExtensionAPI) {
	// -----------------------------------------------------------------------
	// web_search
	// -----------------------------------------------------------------------

	pi.registerTool({
		name: "web_search",
		label: "Web Search",
		description:
			"Search the web using the configured provider (DuckDuckGo by default, optionally Parallel). " +
			"Returns ranked results with title, URL, and snippet. " +
			"Use web_fetch on promising URLs to read full content.",
		promptSnippet: "Search the web for articles, docs, papers, and references",
		promptGuidelines: [
			"Use web_search to find relevant articles, documentation, papers, and references when you need external knowledge.",
			"After searching, pick the most promising URLs and use web_fetch to read their full content.",
			"Prefer specific, targeted queries over broad ones for better results.",
		],
		parameters: Type.Object({
			query: Type.String({ description: "Search query", maxLength: 500 }),
		}),

		async execute(_toolCallId, params, signal, _onUpdate, ctx) {
			if (signal?.aborted) throw new Error("Search cancelled");
			const searchSessionId = ctx.sessionManager.getSessionId();
			const result = await pi.exec("python3", [PYTHON_SCRIPT, "search-session", searchSessionId, params.query], {
				signal,
				timeout: 25_000,
			});
			if (signal?.aborted) throw new Error("Search cancelled");
			if (result.killed) throw new Error("Search timed out");

			let parsed: { results?: Array<{ title: string; url: string; snippet: string }>; error?: string; provider?: string };
			try {
				parsed = JSON.parse(result.stdout);
			} catch {
				throw new Error(`Failed to parse search output: ${result.stdout.slice(0, 300)}`);
			}

			if (parsed.error) throw new Error(`Search error: ${parsed.error}`);
			if (result.code !== 0) throw new Error(`Search failed: ${result.stderr || "unknown error"}`);

			const results = parsed.results ?? [];
			const formatted = results
				.map((r, i) => `${i + 1}. ${r.title}\n   ${r.url}\n   ${r.snippet}`)
				.join("\n\n");

			return {
				content: [{ type: "text", text: formatted || "No results found." }],
				details: { query: params.query, provider: parsed.provider ?? "duckduckgo", resultCount: results.length, results },
			};
		},
	});

	// -----------------------------------------------------------------------
	// web_fetch
	// -----------------------------------------------------------------------

	pi.registerTool({
		name: "web_fetch",
		label: "Web Fetch",
		description:
			"Fetch a web page and return cleaned plain text (HTML tags stripped). " +
			"Large pages are chunked — use offset/limit to paginate. " +
			"Results are cached for 24 hours.",
		promptSnippet: "Fetch and read web page content (HTML auto-cleaned to plain text)",
		promptGuidelines: [
			"Use web_fetch to read the full content of URLs found via web_search or provided by the user.",
			"The first call returns the beginning of the page. Check the response for 'More available' and use offset to continue reading if needed.",
			"Pages are cached for 24 hours — repeated fetches of the same URL are free.",
		],
		parameters: Type.Object({
			url: Type.String({ description: "URL to fetch" }),
			offset: Type.Optional(Type.Number({ description: "Character offset to start reading from (default: 0)" })),
			limit: Type.Optional(
				Type.Number({ description: `Max characters to return (default: ${DEFAULT_CHUNK})` }),
			),
		}),

		async execute(_toolCallId, params, signal) {
			const offset = params.offset ?? 0;
			const limit = params.limit ?? DEFAULT_CHUNK;

			if (!Number.isInteger(offset) || offset < 0 || offset > MAX_OFFSET) {
				throw new Error(`offset must be an integer between 0 and ${MAX_OFFSET}`);
			}
			if (!Number.isInteger(limit) || limit < 1 || limit > MAX_CHUNK) {
				throw new Error(`limit must be an integer between 1 and ${MAX_CHUNK}`);
			}

			// Try cache first
			let entry = getCached(params.url);
			let cached = Boolean(entry);

			if (!entry) {
				const result = await pi.exec("python3", [PYTHON_SCRIPT, "fetch", params.url], {
					signal,
					timeout: 30_000,
				});

				if (result.code !== 0) {
					throw new Error(`Fetch failed: ${result.stderr || "unknown error"}`);
				}

				let parsed: {
					url?: string;
					title?: string;
					content?: string;
					size?: number;
					contentType?: string;
					error?: string;
				};
				try {
					parsed = JSON.parse(result.stdout);
				} catch {
					throw new Error(`Failed to parse fetch output: ${result.stdout.slice(0, 300)}`);
				}

				if (parsed.error) throw new Error(`Fetch error: ${parsed.error}`);

				entry = {
					url: parsed.url ?? params.url,
					title: parsed.title ?? "",
					content: parsed.content ?? "",
					size: parsed.size ?? (parsed.content ?? "").length,
					timestamp: Date.now(),
					contentType: parsed.contentType,
				};

				await putCacheAsync(entry);
				cached = false;
			}

			const chunk = entry.content.slice(offset, offset + limit);
			const hasMore = offset + limit < entry.size;

			// Build a concise header the LLM can use
			const headerLines: string[] = [];
			if (entry.title) headerLines.push(`Title: ${entry.title}`);
			headerLines.push(`URL: ${entry.url}`);
			headerLines.push(`Content: ${entry.size} chars total`);
			if (offset > 0 || hasMore) {
				headerLines.push(`Showing: chars ${offset}–${offset + chunk.length} of ${entry.size}`);
			}
			if (hasMore) {
				headerLines.push(`More available — use offset=${offset + limit} to continue`);
			}
			headerLines.push("---");

			return {
				content: [{ type: "text", text: headerLines.join("\n") + "\n" + chunk }],
				details: {
					url: entry.url,
					title: entry.title,
					totalSize: entry.size,
					offset,
					chunkSize: chunk.length,
					hasMore,
					cached,
				},
			};
		},
	});

	// -----------------------------------------------------------------------
	// github
	// -----------------------------------------------------------------------

	pi.registerTool({
		name: "github",
		label: "GitHub",
		description:
			"Search GitHub repositories, code, and issues. Read files and list repo structure. Requires the gh CLI to be installed and authenticated.",
		promptSnippet: "Search GitHub repos/code/issues, read files, and list repo trees via gh CLI",
		promptGuidelines: [
			"Use the github tool for GitHub-specific research: finding repos, reading source code, exploring issues and discussions.",
			"For search_repos, use GitHub search qualifiers like 'language:rust stars:>100 topic:async'.",
			"Use list_tree first to understand a repo's structure, then read_file for specific files.",
			"For read_file, you can omit branch to use the default branch (HEAD).",
		],
		parameters: Type.Object({
			action: StringEnum(
				["search_repos", "search_code", "search_issues", "read_file", "list_tree"] as const,
				{ description: "Action to perform" },
			),
			query: Type.Optional(Type.String({ description: "Search query (for search_* actions)", maxLength: 500 })),
			owner: Type.Optional(Type.String({ description: "Repo owner (for read_file, list_tree)" })),
			repo: Type.Optional(Type.String({ description: "Repo name (for read_file, list_tree)" })),
			path: Type.Optional(
				Type.String({ description: "File path for read_file, or path prefix filter for list_tree" }),
			),
			branch: Type.Optional(Type.String({ description: "Branch/tag/commit (default: HEAD)" })),
		}),

		async execute(_toolCallId, params, signal) {
			// Check gh is available (fast — cached after first run)
			const ghCheck = await pi.exec("gh", ["--version"], { timeout: 5_000 });
			if (ghCheck.code !== 0) {
				throw new Error(
					"gh CLI is not installed or not in PATH. Install from https://cli.github.com and run 'gh auth login'.",
				);
			}

			switch (params.action) {
				// ---- search_repos ----
				case "search_repos": {
					if (!params.query) throw new Error("query is required for search_repos");
					const r = await pi.exec(
						"gh",
						[
							"search",
							"repos",
							params.query,
							"--json",
							"fullName,description,url,stargazersCount,language,updatedAt",
							"--limit",
							"10",
						],
						{ signal, timeout: 15_000 },
					);
					if (r.code !== 0) throw new Error(`gh search repos failed: ${r.stderr}`);

					const repos = JSON.parse(r.stdout) as Array<{
						fullName: string;
						description: string;
						url: string;
						stargazersCount: number;
						language: string;
						updatedAt: string;
					}>;
					const text = repos
						.map(
							(repo) =>
								`${repo.fullName} (★${repo.stargazersCount}${repo.language ? `, ${repo.language}` : ""})\n  ${repo.url}\n  ${repo.description || "(no description)"}`,
						)
						.join("\n\n");

					return {
						content: [{ type: "text", text: text || "No repositories found." }],
						details: { action: params.action, repos },
					};
				}

				// ---- search_code ----
				case "search_code": {
					if (!params.query) throw new Error("query is required for search_code");
					const r = await pi.exec(
						"gh",
						["search", "code", params.query, "--json", "repository,path,textMatches", "--limit", "10"],
						{ signal, timeout: 15_000 },
					);
					if (r.code !== 0) throw new Error(`gh search code failed: ${r.stderr}`);

					const matches = JSON.parse(r.stdout) as Array<{
						repository: { fullName?: string; name?: string };
						path: string;
						textMatches: Array<{ fragment?: string }>;
					}>;
					const text = matches
						.map((m) => {
							const repo = m.repository?.fullName || m.repository?.name || "unknown";
							const snippets = (m.textMatches || [])
								.map((t) => t.fragment || "")
								.filter(Boolean)
								.slice(0, 2)
								.join("\n  ---\n  ");
							return `${repo}: ${m.path}${snippets ? `\n  ${snippets}` : ""}`;
						})
						.join("\n\n");

					return {
						content: [{ type: "text", text: text || "No code matches found." }],
						details: { action: params.action, matches },
					};
				}

				// ---- search_issues ----
				case "search_issues": {
					if (!params.query) throw new Error("query is required for search_issues");
					const r = await pi.exec(
						"gh",
						[
							"search",
							"issues",
							params.query,
							"--json",
							"title,url,state,repository,updatedAt",
							"--limit",
							"10",
						],
						{ signal, timeout: 15_000 },
					);
					if (r.code !== 0) throw new Error(`gh search issues failed: ${r.stderr}`);

					const issues = JSON.parse(r.stdout) as Array<{
						title: string;
						url: string;
						state: string;
						repository: { fullName?: string; name?: string };
						updatedAt: string;
					}>;
					const text = issues
						.map((i) => {
							const repo = i.repository?.fullName || i.repository?.name || "";
							return `[${i.state}] ${i.title}\n  ${repo} | ${i.url}`;
						})
						.join("\n\n");

					return {
						content: [{ type: "text", text: text || "No issues found." }],
						details: { action: params.action, issues },
					};
				}

				// ---- read_file ----
				case "read_file": {
					if (!params.owner || !params.repo) throw new Error("owner and repo are required for read_file");
					if (!params.path) throw new Error("path is required for read_file");
					if (params.path.includes("..") || params.path.startsWith("/"))
						throw new Error("Invalid path: must not contain '..' or start with '/'");

					const ref = params.branch ? `?ref=${encodeURIComponent(params.branch)}` : "";
					const endpoint = `repos/${params.owner}/${params.repo}/contents/${params.path}${ref}`;
					const r = await pi.exec("gh", ["api", endpoint, "-H", "Accept: application/vnd.github.raw+json"], {
						signal,
						timeout: 20_000,
					});
					if (r.code !== 0) throw new Error(`gh api failed: ${r.stderr}`);

					const content = r.stdout;
					const header = [
						`File: ${params.owner}/${params.repo}/${params.path}${params.branch ? ` @ ${params.branch}` : ""}`,
						`Size: ${content.length} chars`,
						"---",
					].join("\n");

					return {
						content: [{ type: "text", text: `${header}\n${content}` }],
						details: {
							action: params.action,
							owner: params.owner,
							repo: params.repo,
							path: params.path,
							branch: params.branch,
							size: content.length,
						},
					};
				}

				// ---- list_tree ----
				case "list_tree": {
					if (!params.owner || !params.repo)
						throw new Error("owner and repo are required for list_tree");
					if (params.path && (params.path.includes("..") || params.path.startsWith("/")))
						throw new Error("Invalid path: must not contain '..' or start with '/'");

					const branch = params.branch || "HEAD";
					const endpoint = `repos/${params.owner}/${params.repo}/git/trees/${encodeURIComponent(branch)}?recursive=1`;
					const r = await pi.exec("gh", ["api", endpoint], { signal, timeout: 20_000 });
					if (r.code !== 0) throw new Error(`gh api failed: ${r.stderr}`);

					const data = JSON.parse(r.stdout) as {
						tree: Array<{ path: string; type: string; size?: number }>;
						truncated?: boolean;
					};

					let files = (data.tree || []).filter((e) => e.type === "blob");

					// Apply path prefix filter
					if (params.path) {
						const prefix = params.path.endsWith("/") ? params.path : `${params.path}/`;
						files = files.filter((f) => f.path.startsWith(prefix) || f.path === params.path);
					}

					const totalFiles = files.length;
					const truncated = totalFiles > MAX_TREE_ENTRIES;
					if (truncated) files = files.slice(0, MAX_TREE_ENTRIES);

					const tree = files.map((f) => `${f.path}  (${f.size ?? 0}b)`).join("\n");

					const headerLines = [
						`Repository: ${params.owner}/${params.repo} @ ${branch}`,
						`Files: ${totalFiles}${truncated ? ` (showing first ${MAX_TREE_ENTRIES})` : ""}`,
					];
					if (params.path) headerLines.push(`Filter: ${params.path}`);
					if (data.truncated) headerLines.push("Note: GitHub API truncated the full tree");
					headerLines.push("---");

					return {
						content: [{ type: "text", text: `${headerLines.join("\n")}\n${tree}` }],
						details: {
							action: params.action,
							owner: params.owner,
							repo: params.repo,
							branch,
							fileCount: totalFiles,
							truncated,
							pathFilter: params.path,
						},
					};
				}

				default:
					throw new Error(`Unknown action: ${params.action}`);
			}
		},
	});
}
