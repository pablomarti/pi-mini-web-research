#!/usr/bin/env python3
"""
Web research helper for pi-mini-web-research.
Python3 stdlib only — no pip packages required.

Commands:
    search <query>   Search the configured provider, return JSON results
    fetch  <url>     Fetch URL, strip HTML, return cleaned text as JSON
"""

import json
import ipaddress
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
TIMEOUT = 20
MAX_DOWNLOAD = 1024 * 1024  # 1 MB

ALLOWLIST_ENV = "PI_WEB_RESEARCH_ALLOWLIST"
SEARCH_PROVIDER_ENV = "PI_WEB_RESEARCH_SEARCH_PROVIDER"
PARALLEL_MCP_URL = "https://search.parallel.ai/mcp"
PROJECT_USER_AGENT = (
    "pi-mini-web-research/1.0.0 "
    "(+https://github.com/pablomarti/pi-mini-web-research)"
)

BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
    "metadata",
    "instance-data",
    "169.254.169.254",
    "100.100.100.200",  # Alibaba Cloud metadata
}

BLOCKED_HOST_SUFFIXES = (
    ".localhost",
    ".localdomain",
    ".local",
    ".internal",
    ".home.arpa",
)


def _ssl_context():
    """Create SSL context, working around macOS Python missing certs."""
    ctx = ssl.create_default_context()
    # macOS framework Python often lacks root certs. Try system CA bundle.
    if not ctx.get_ca_certs():
        for ca_path in [
            "/etc/ssl/cert.pem",  # macOS system
            "/etc/ssl/certs/ca-certificates.crt",  # Debian/Ubuntu
            "/etc/pki/tls/certs/ca-bundle.crt",  # RHEL/Fedora
            "/opt/homebrew/etc/openssl@3/cert.pem",  # Homebrew
        ]:
            if os.path.isfile(ca_path):
                ctx.load_verify_locations(ca_path)
                break
        if not ctx.get_ca_certs():
            raise ssl.SSLError(
                "No CA certificates found. Run: pip install certifi, or set SSL_CERT_FILE."
            )
    return ctx


def _parse_allowlist():
    """Parse PI_WEB_RESEARCH_ALLOWLIST into allowed hosts and networks."""
    raw = os.environ.get(ALLOWLIST_ENV, "")
    hosts = set()
    networks = []

    for item in raw.split(","):
        token = item.strip().lower()
        if not token:
            continue

        try:
            if "/" in token:
                networks.append(ipaddress.ip_network(token, strict=False))
                continue

            ip = ipaddress.ip_address(token)
            prefix = 32 if ip.version == 4 else 128
            networks.append(ipaddress.ip_network(f"{token}/{prefix}", strict=False))
            continue
        except ValueError:
            hosts.add(token)

    return hosts, networks


def _host_is_allowlisted(hostname: str, allowed_hosts: set[str]) -> bool:
    host = hostname.lower().rstrip(".")
    if host in allowed_hosts:
        return True

    for allowed in allowed_hosts:
        if host.endswith(f".{allowed}"):
            return True

    return False


def _ip_is_allowlisted(ip: ipaddress._BaseAddress, allowed_networks) -> bool:
    return any(ip in network for network in allowed_networks)


def _is_blocked_hostname(hostname: str) -> bool:
    host = hostname.lower().rstrip(".")
    if host in BLOCKED_HOSTNAMES:
        return True
    return any(host.endswith(suffix) for suffix in BLOCKED_HOST_SUFFIXES)


def _is_blocked_ip(ip: ipaddress._BaseAddress) -> bool:
    # Strict-by-default: only globally routable addresses are allowed.
    return not ip.is_global


def _resolve_ips_for_host(hostname: str):
    host = hostname.lower().rstrip(".")
    try:
        parsed = ipaddress.ip_address(host)
        return [parsed]
    except ValueError:
        pass

    results = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    ips = []
    seen = set()
    for result in results:
        value = result[4][0]
        if "%" in value:
            value = value.split("%", 1)[0]
        ip = ipaddress.ip_address(value)
        key = str(ip)
        if key not in seen:
            seen.add(key)
            ips.append(ip)

    if not ips:
        raise ValueError(f"Could not resolve host: {hostname}")

    return ips


def _validate_target_url(url: str):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Only HTTP(S) URLs are supported, got: {parsed.scheme!r}")
    if not parsed.hostname:
        raise ValueError("URL must include a hostname")
    if parsed.username or parsed.password:
        raise ValueError("URLs with embedded credentials are not allowed")

    hostname = parsed.hostname.lower().rstrip(".")
    allowed_hosts, allowed_networks = _parse_allowlist()

    host_allowlisted = _host_is_allowlisted(hostname, allowed_hosts)
    if _is_blocked_hostname(hostname) and not host_allowlisted:
        raise ValueError(
            "Target host is blocked by default network policy. "
            f"Use {ALLOWLIST_ENV} to allowlist trusted internal hosts."
        )

    for ip in _resolve_ips_for_host(hostname):
        if _is_blocked_ip(ip) and not (
            host_allowlisted or _ip_is_allowlisted(ip, allowed_networks)
        ):
            raise ValueError(
                "Target resolves to a blocked IP address. "
                f"Use {ALLOWLIST_ENV} to allowlist trusted internal ranges."
            )


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_target_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _request(url, data=None, headers=None, method=None):
    """Build and execute a urllib request."""
    _validate_target_url(url)

    hdrs = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    opener = urllib.request.build_opener(
        _SafeRedirectHandler(),
        urllib.request.HTTPSHandler(context=_ssl_context()),
    )
    opener.addheaders = []
    return opener.open(req, timeout=TIMEOUT)


# ---------------------------------------------------------------------------
# HTML → plain text
# ---------------------------------------------------------------------------


class _TextExtractor(HTMLParser):
    """Extract readable text from HTML, skipping scripts/styles/nav."""

    _SKIP = frozenset({"script", "style", "noscript", "svg", "path", "nav"})
    _BLOCK = frozenset(
        {
            "p",
            "div",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "li",
            "tr",
            "br",
            "hr",
            "blockquote",
            "pre",
            "article",
            "section",
            "header",
            "footer",
            "table",
            "thead",
            "tbody",
            "dt",
            "dd",
            "figcaption",
        }
    )

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.title = ""
        self._skip_depth = 0
        self._in_title = False

    # --- parser callbacks ---

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self._SKIP:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        if tag == "title":
            self._in_title = False
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._skip_depth == 0:
            self.parts.append(data)

    # --- output ---

    def get_text(self) -> str:
        text = "".join(self.parts)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n[ \t]+", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


# ---------------------------------------------------------------------------
# DuckDuckGo Lite parser
# ---------------------------------------------------------------------------


class _DDGParser(HTMLParser):
    """Parse DuckDuckGo Lite result rows."""

    def __init__(self):
        super().__init__()
        self.results: list[dict] = []
        self._cur: dict = {}
        self._in_link = False
        self._in_snippet = False
        self._link_text = ""
        self._snippet_text = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and a.get("class") == "result-link":
            self._in_link = True
            self._cur["url"] = a.get("href", "")
            self._link_text = ""
        elif tag == "td":
            if "result-snippet" in a.get("class", ""):
                self._in_snippet = True
                self._snippet_text = ""

    def handle_endtag(self, tag):
        if tag == "a" and self._in_link:
            self._in_link = False
            self._cur["title"] = self._link_text.strip()
        elif tag == "td" and self._in_snippet:
            self._in_snippet = False
            self._cur["snippet"] = self._snippet_text.strip()
            if self._cur.get("url") and self._cur.get("title"):
                self.results.append(self._cur)
            self._cur = {}

    def handle_data(self, data):
        if self._in_link:
            self._link_text += data
        if self._in_snippet:
            self._snippet_text += data


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _mcp_response(resp, request_id):
    """Read one bounded JSON-RPC response, including Streamable HTTP SSE."""
    if "text/event-stream" in resp.headers.get("Content-Type", "").lower():
        size = 0
        data = []
        while True:
            line = resp.readline(MAX_DOWNLOAD + 1)
            size += len(line)
            if size > MAX_DOWNLOAD:
                raise ValueError("Parallel response exceeds 1 MB")
            if not line or not line.strip():
                if data:
                    message = json.loads("\n".join(data))
                    if isinstance(message, dict) and message.get("id") == request_id:
                        return message
                data = []
                if not line:
                    raise ValueError("Parallel stream ended without a response")
            elif line.startswith(b"data:"):
                data.append(line[5:].decode("utf-8").lstrip(" ").rstrip("\r\n"))
    raw = resp.read(MAX_DOWNLOAD + 1)
    if len(raw) > MAX_DOWNLOAD:
        raise ValueError("Parallel response exceeds 1 MB")
    return json.loads(raw)


class _ParallelRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Parallel MCP redirects are not allowed")


def _parallel_request(payload, headers, timeout):
    # The destination is fixed, not an untrusted URL. Refuse all redirects so
    # MCP session headers can never be forwarded to another service.
    request = urllib.request.Request(
        PARALLEL_MCP_URL, data=json.dumps(payload).encode("utf-8"),
        headers=headers, method="POST",
    )
    opener = urllib.request.build_opener(
        _ParallelRedirectHandler(), urllib.request.HTTPSHandler(context=_ssl_context())
    )
    opener.addheaders = []
    return opener.open(request, timeout=timeout)


def _parallel_search(query: str, session_id=None):
    """Use anonymous Parallel Search MCP with a single overall time budget."""
    deadline = time.monotonic() + TIMEOUT
    headers = {
        "User-Agent": PROJECT_USER_AGENT,
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }

    def rpc(method, params=None, request_id=None):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Parallel search timed out")
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if request_id is not None:
            payload["id"] = request_id
        with _parallel_request(payload, headers, remaining) as resp:
            if method == "initialize":
                session = resp.headers.get("Mcp-Session-Id")
                if session:
                    headers["Mcp-Session-Id"] = session
            if request_id is None:
                return None
            message = _mcp_response(resp, request_id)
        if not isinstance(message, dict) or message.get("id") != request_id:
            raise ValueError("Invalid Parallel JSON-RPC response")
        if "error" in message:
            raise ValueError(f"Parallel MCP error: {message['error']}")
        if not isinstance(message.get("result"), dict):
            raise ValueError("Missing Parallel MCP result")
        return message["result"]

    initialized = rpc("initialize", {
        "protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "pi-mini-web-research", "version": "1.0.0"},
    }, 1)
    version = initialized.get("protocolVersion")
    if version != "2025-03-26":
        raise ValueError(f"Unsupported Parallel MCP protocol: {version}")
    headers["MCP-Protocol-Version"] = version
    rpc("notifications/initialized")
    arguments = {"objective": query, "search_queries": [query]}
    if session_id:
        arguments["session_id"] = session_id
    result = rpc("tools/call", {"name": "web_search", "arguments": arguments}, 2)
    if result.get("isError"):
        text = " ".join(c.get("text", "") for c in result.get("content", [])
                        if c.get("type") == "text")
        raise ValueError(f"Parallel search failed: {text[:1000]}")
    structured = result.get("structuredContent")
    if structured is None:
        text = "\n".join(c.get("text", "") for c in result.get("content", [])
                         if c.get("type") == "text")
        structured = json.loads(text)
    if not isinstance(structured, dict) or not isinstance(structured.get("results"), list):
        raise ValueError("Invalid Parallel search results")
    results = []
    for item in structured["results"][:20]:
        if not isinstance(item, dict):
            raise ValueError("Invalid Parallel search result")
        url = item.get("url", "")
        parsed_url = urllib.parse.urlparse(url) if isinstance(url, str) else None
        if (not parsed_url or parsed_url.scheme not in ("http", "https")
                or not parsed_url.hostname or parsed_url.username or parsed_url.password):
            raise ValueError("Invalid Parallel result URL")
        title = item.get("title") or url
        excerpts = item.get("excerpts", [])
        if not isinstance(title, str) or not isinstance(excerpts, list) or not all(isinstance(e, str) for e in excerpts):
            raise ValueError("Invalid Parallel search excerpt")
        results.append({"title": title[:500], "url": url[:2000],
                        "snippet": "\n".join(excerpts)[:3000]})
    return results


def cmd_search(query: str, session_id=None):
    """Resolve an explicit provider, preserving the keyless DuckDuckGo default."""
    provider = os.environ.get(SEARCH_PROVIDER_ENV, "duckduckgo").strip().lower()
    if provider == "none":
        raise ValueError(f"Web search is disabled by {SEARCH_PROVIDER_ENV}=none")
    if provider == "parallel":
        json.dump({"results": _parallel_search(query, session_id), "provider": provider},
                  sys.stdout, ensure_ascii=False)
        return
    if provider != "duckduckgo":
        raise ValueError(f"Unknown {SEARCH_PROVIDER_ENV}: {provider!r}; use duckduckgo, parallel, or none")
    _search_duckduckgo(query)


def _search_duckduckgo(query: str):
    """Search DuckDuckGo Lite and print JSON results."""
    body = urllib.parse.urlencode({"q": query}).encode()
    resp = _request(
        "https://lite.duckduckgo.com/lite",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    html = resp.read().decode("utf-8", errors="replace")

    parser = _DDGParser()
    parser.feed(html)

    # deduplicate by URL
    seen: set[str] = set()
    unique: list[dict] = []
    for r in parser.results:
        if r["url"] not in seen:
            seen.add(r["url"])
            unique.append(r)

    json.dump({"results": unique[:20]}, sys.stdout, ensure_ascii=False)


def cmd_fetch(url: str):
    """Fetch a URL, clean HTML, print JSON with text content."""
    try:
        resp = _request(url)
        raw = resp.read(MAX_DOWNLOAD)
        content_type = resp.headers.get("Content-Type", "")
        final_url = resp.geturl()
        _validate_target_url(final_url)
    except (urllib.error.URLError, OSError) as exc:
        json.dump({"error": str(exc), "url": url}, sys.stdout)
        return
    except ValueError as exc:
        json.dump({"error": str(exc), "url": url}, sys.stdout)
        return

    # detect encoding
    encoding = "utf-8"
    if "charset=" in content_type:
        encoding = content_type.split("charset=")[-1].split(";")[0].strip()

    text = raw.decode(encoding, errors="replace")

    is_html = "html" in content_type.lower() or text.strip()[:200].lower().startswith(
        ("<!doctype", "<html")
    )

    if is_html:
        ext = _TextExtractor()
        try:
            ext.feed(text)
        except Exception:
            pass  # partial parse is fine
        cleaned = ext.get_text()
        title = ext.title.strip()
    else:
        cleaned = text
        title = ""

    json.dump(
        {
            "url": final_url,
            "title": title,
            "content": cleaned,
            "size": len(cleaned),
            "contentType": content_type,
        },
        sys.stdout,
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        json.dump({"error": "Usage: research.py <search|fetch> <args>"}, sys.stdout)
        sys.exit(1)

    cmd = sys.argv[1]
    try:
        if cmd in ("search", "search-session"):
            query_start = 3 if cmd == "search-session" else 2
            if len(sys.argv) <= query_start:
                json.dump({"error": "Missing search query"}, sys.stdout)
                sys.exit(1)
            session_id = sys.argv[2] if cmd == "search-session" else None
            cmd_search(" ".join(sys.argv[query_start:]), session_id)
        elif cmd == "fetch":
            if len(sys.argv) < 3:
                json.dump({"error": "Missing URL"}, sys.stdout)
                sys.exit(1)
            cmd_fetch(sys.argv[2])
        else:
            json.dump({"error": f"Unknown command: {cmd}"}, sys.stdout)
            sys.exit(1)
    except Exception as exc:
        json.dump({"error": str(exc)}, sys.stdout)
        sys.exit(1)
