"""Subprocess-only HTTP fixture. Activated explicitly by the Node integration test."""
import io
import json
import os
import socket
import time
import urllib.request
from email.message import Message


class Response(io.BytesIO):
    def __init__(self, data, content_type="application/json", session=False):
        super().__init__(data)
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        if session:
            self.headers["Mcp-Session-Id"] = "fixture-mcp-session"


def fixture_open(self, request, timeout=None):
    payload = json.loads(request.data) if request.headers.get("Content-type") == "application/json" else None
    record = {"url": request.full_url, "headers": dict(request.header_items()),
              "payload": payload, "timeout": timeout}
    with open(os.environ["PI_TEST_REQUESTS"], "a") as log:
        log.write(json.dumps(record) + "\n")
    if payload is None:
        return Response(b'<a class="result-link" href="https://example.com/ddg">DDG result</a>'
                        b'<td class="result-snippet">Original provider</td>', "text/html")
    method = payload["method"]
    if method == "notifications/initialized":
        return Response(b"")
    if method == "initialize":
        result = {"protocolVersion": "2025-03-26"}
    else:
        mode = os.environ.get("PI_TEST_MODE", "json")
        if mode == "stall":
            time.sleep(30)
        if mode == "timeout":
            raise TimeoutError("fixture socket timed out")
        if mode == "malformed":
            return Response(b"not JSON")
        if mode == "error":
            result = {"isError": True, "content": [{"type": "text", "text": "fixture provider failure"}]}
        else:
            data = {"results": [{"title": "Reference", "url": "https://example.com/reference?q=a%26b",
                                 "excerpts": ["Useful evidence", "Second excerpt"]}]}
            if mode == "text":
                result = {"content": [{"type": "text", "text": json.dumps(data)}]}
            else:
                result = {"structuredContent": data}
    message = json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": result}).encode()
    if os.environ.get("PI_TEST_MODE") == "sse":
        return Response(b': keepalive\n\nevent: message\ndata: ' + message + b'\n\n',
                        "text/event-stream", method == "initialize")
    return Response(message, session=method == "initialize")


if os.environ.get("PI_TEST_REQUESTS"):
    urllib.request.OpenerDirector.open = fixture_open
    # No real DNS or HTTP is needed for this deterministic subprocess fixture.
    socket.getaddrinfo = lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
