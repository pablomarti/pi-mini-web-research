# pi-mini-web-research

Web research tools for [pi](https://github.com/badlogic/pi-mono). Gives the agent the ability to search the web, read web pages, and explore GitHub — all without installing any npm dependencies.

## Tools

| Tool | Description |
|------|-------------|
| `web_search` | Search the web via DuckDuckGo Lite. Returns ranked results with title, snippet, and URL. |
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
- **No API keys stored** — DuckDuckGo needs none; `gh` uses your existing authentication
- **Local cache only** — nothing leaves your machine except the HTTP requests you'd make manually
- **No telemetry** — no tracking, no analytics, no phone-home

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

## License

MIT
