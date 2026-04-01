#!/usr/bin/env python3
"""
Web research helper for pi-mini-web-research.
Python3 stdlib only — no pip packages required.

Commands:
    search <query>   Search DuckDuckGo Lite, return JSON results
    fetch  <url>     Fetch URL, strip HTML, return cleaned text as JSON
"""

import json
import ipaddress
import os
import re
import socket
import ssl
import sys
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


def cmd_search(query: str):
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
        if cmd == "search":
            if len(sys.argv) < 3:
                json.dump({"error": "Missing search query"}, sys.stdout)
                sys.exit(1)
            cmd_search(" ".join(sys.argv[2:]))
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
