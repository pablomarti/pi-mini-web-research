# pi-mini-web-research

Web research tools for [pi](https://github.com/badlogic/pi-mono). Gives the agent the ability to search the web, read web pages, and explore GitHub — all without installing any npm dependencies.

## Tools

| Tool | Description |
|------|-------------|
| `web_search` | Search the web via DuckDuckGo Lite or opt-in Parallel Search MCP. Returns ranked results with title, snippet, and URL. |
| `web_fetch` | Fetch a URL and return cleaned plain text (HTML tags stripped). Supports pagination via offset/limit for large pages. Results cached 24h. |
| `github` | Search GitHub repos/code/issues, read files, and list repo trees via the `gh` CLI. |

## Prerequisites

- **Python 3.10+** — used for web fetching and HTML cleaning (stdlib only, no pip packages)
- **`gh` CLI** (optional) — required only for the `github` tool. Install from [cli.github.com](https://cli.github.com) and run `gh auth login`.

Both come pre-installed or are easily available on macOS and Linux.

## Install

```bash
# From git
pi install git:github.com/pablomarti/pi-mini-web-research

# From a local clone
pi install /path/to/pi-mini-web-research

# Or just test it without installing
pi -e /path/to/pi-mini-web-research/extensions/web-research
```

## How It Works

### web_search

Queries DuckDuckGo Lite (`lite.duckduckgo.com/lite`) via a Python3 script using only `urllib` and `html.parser`. No API keys needed. Returns up to 20 results with title, URL, and snippet.

To use [Parallel Search MCP](https://docs.parallel.ai/integrations/mcp/search-mcp)
instead, set the provider before starting pi:

```bash
PI_WEB_RESEARCH_SEARCH_PROVIDER=parallel pi
```

Parallel supplies search excerpts through its anonymous HTTP MCP endpoint
(`https://search.parallel.ai/mcp`), using Fast mode with no account, API key,
or additional dependencies. Queries are sent to Parallel only when selected.
Anonymous access has service rate limits; errors are reported to the agent
without switching providers. Search retains pi's cancellation and 25-second
process timeout. `web_fetch` continues to fetch pages directly with the existing
network policy and cache.

Leave `PI_WEB_RESEARCH_SEARCH_PROVIDER` unset, or set it to `duckduckgo`, to keep
DuckDuckGo Lite. Set it to `none` to disable search requests. Unknown values
produce an error. These settings do not enable a tool disabled in pi.

### web_fetch

Fetches any URL via Python3 `urllib`, strips HTML to plain text using `html.parser`, and returns the cleaned content. Key features:

- **Smart chunking**: Returns 12,000 chars by default. Use `offset` and `limit` to paginate through longer pages without wasting tokens.
- **24-hour cache**: Fetched pages are cached at `~/.pi/agent/web-research-cache.json`. Repeat visits are instant and free.
- **Token efficient**: Raw HTML (often 200KB+) is cleaned to just the readable text (typically 5–20KB).

### github

Uses the `gh` CLI for structured, token-efficient GitHub access:

| Action | What it does |
|--------|-------------|
| `search_repos` | Search repositories with GitHub qualifiers (`language:`, `stars:>`, `topic:`, etc.) |
| `search_code` | Search code across all public repos |
| `search_issues` | Search issues and pull requests |
| `read_file` | Read a specific file from a repo (any branch/tag/commit) |
| `list_tree` | List all files in a repo with optional path prefix filter |

## Security

- **Zero npm dependencies** — only Python3 stdlib and `gh` CLI
- **Strict network policy by default** — `web_fetch` blocks localhost, private/link-local networks, and known cloud metadata endpoints
- **Explicit allowlist escape hatch** — set `PI_WEB_RESEARCH_ALLOWLIST` for trusted internal hosts or CIDR ranges
- **No API keys stored** — DuckDuckGo needs none; `gh` uses your existing authentication
- **Local cache only** — nothing leaves your machine except the HTTP requests you'd make manually
- **No telemetry** — no tracking, no analytics, no phone-home

### Internal Host Allowlist

`web_fetch` is locked down to public network targets unless allowlisted.

```bash
# Allow a trusted internal host and subnet
export PI_WEB_RESEARCH_ALLOWLIST="docs.corp.example,10.0.0.0/8"
```

Allowlist entries can be:

- hostnames (exact match or subdomains)
- IP addresses
- CIDR ranges

## Cache

The fetch cache lives at `~/.pi/agent/web-research-cache.json`:

- Entries expire after **24 hours**
- Max **200 entries** (oldest pruned first)
- Delete the file to clear the cache

## Example Usage

Once installed, just ask the agent naturally:

```
Search for best practices for error handling in Rust 2025
```

```
Look at the tokio repository on GitHub and read their README
```

```
Find recent papers about transformer architecture improvements
```

The agent will use `web_search` → `web_fetch` and/or `github` tools autonomously.

## Development

Run the Python tests with `python3 -m unittest discover -s tests -v`.
The extension integration test uses an installed pi package and exercises its
real loader and subprocess executor with deterministic HTTP fixtures:

```bash
PI_CODING_AGENT_DIR=/path/to/node_modules/@mariozechner/pi-coding-agent \
  node tests/test_extension.mjs
```

Set `PI_TEST_PROCESS_TIMEOUT=1` to include the 25-second subprocess timeout check.
The fixtures make no model or external HTTP requests.

## License

MIT
