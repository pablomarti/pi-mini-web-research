import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
from unittest import TestCase, mock

SPEC = importlib.util.spec_from_file_location(
    "research_parallel", Path(__file__).resolve().parents[1] / "extensions/web-research/research.py"
)
research = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(research)


class Response(io.BytesIO):
    def __init__(self, message, content_type="application/json", raw=False):
        super().__init__(message if raw else json.dumps(message).encode())
        self.headers = {"Content-Type": content_type, "Mcp-Session-Id": "test-session"}


class ParallelSearchTests(TestCase):
    def response(self, result, request_id=2):
        return Response({"jsonrpc": "2.0", "id": request_id, "result": result})

    def search(self, tool_result):
        replies = [self.response({"protocolVersion": "2025-03-26"}, 1),
                   Response(b"", raw=True), self.response(tool_result)]
        with mock.patch.object(research, "_parallel_request", side_effect=replies):
            return research._parallel_search("query", "stable-session")

    def test_empty_results(self):
        self.assertEqual(self.search({"structuredContent": {"results": []}}), [])

    def test_titles_bounds_and_excerpts(self):
        result = self.search({"structuredContent": {"results": [
            {"url": "https://example.com", "title": None, "excerpts": ["x" * 5000]},
        ]}})
        self.assertEqual(result[0]["title"], "https://example.com")
        self.assertEqual(len(result[0]["snippet"]), 3000)

    def test_invalid_urls_and_shapes_fail(self):
        for item in [None, {}, {"url": "file:///etc/passwd"},
                     {"url": "https:///path"}, {"url": "https://user:pass@example.com"},
                     {"url": "https://example.com", "excerpts": "wrong shape"}]:
            with self.subTest(item=item), self.assertRaises(ValueError):
                self.search({"structuredContent": {"results": [item]}})
        with self.assertRaises(ValueError):
            self.search({"structuredContent": {}})

    def test_mcp_errors(self):
        with self.assertRaisesRegex(ValueError, "quota exceeded"):
            self.search({"isError": True, "content": [{"type": "text", "text": "quota exceeded"}]})
        reply = Response({"jsonrpc": "2.0", "id": 1, "error": {"message": "bad protocol"}})
        with mock.patch.object(research, "_parallel_request", return_value=reply):
            with self.assertRaisesRegex(ValueError, "bad protocol"):
                research._parallel_search("query")

    def test_wrong_protocol_and_id(self):
        for reply in [self.response({"protocolVersion": "other"}, 1), self.response({}, 9)]:
            with mock.patch.object(research, "_parallel_request", return_value=reply):
                with self.assertRaises(ValueError):
                    research._parallel_search("query")

    def test_json_and_sse_size_limits(self):
        for content_type in ["application/json", "text/event-stream"]:
            with self.subTest(content_type=content_type):
                reply = Response(b"x" * (research.MAX_DOWNLOAD + 1), content_type, raw=True)
                with self.assertRaisesRegex(ValueError, "exceeds"):
                    research._mcp_response(reply, 1)

    def test_sse_skips_notifications_and_handles_multiline_crlf(self):
        raw = (b': comment\r\n\r\ndata: {"method":"notification"}\r\n\r\n'
               b'event: message\r\ndata: {"id": 7,\r\ndata: "result": {}}\r\n\r\n')
        self.assertEqual(research._mcp_response(Response(raw, "text/event-stream", True), 7),
                         {"id": 7, "result": {}})
        with self.assertRaisesRegex(ValueError, "without a response"):
            research._mcp_response(Response(b":closed\n\n", "text/event-stream", True), 7)

    def test_overall_request_budget(self):
        with mock.patch.object(research.time, "monotonic", side_effect=[0, 21]):
            with mock.patch.object(research, "_parallel_request") as request:
                with self.assertRaises(TimeoutError):
                    research._parallel_search("query")
                request.assert_not_called()

    def test_resolver_preserves_defaults_and_explicit_choices(self):
        for env in [{}, {research.SEARCH_PROVIDER_ENV: "duckduckgo"}]:
            with mock.patch.dict(os.environ, env, clear=True):
                with mock.patch.object(research, "_search_duckduckgo") as ddg:
                    research.cmd_search("q")
                    ddg.assert_called_once_with("q")
        with mock.patch.dict(os.environ, {research.SEARCH_PROVIDER_ENV: "parallel"}, clear=True):
            with mock.patch.object(research, "_parallel_search", return_value=[]) as parallel:
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    research.cmd_search("q", "session")
                parallel.assert_called_once_with("q", "session")
                self.assertEqual(json.loads(out.getvalue()), {"results": [], "provider": "parallel"})
        for provider in ["none", "invalid", ""]:
            with mock.patch.dict(os.environ, {research.SEARCH_PROVIDER_ENV: provider}, clear=True):
                with mock.patch.object(research, "_request") as request:
                    with self.assertRaises(ValueError):
                        research.cmd_search("q")
                    request.assert_not_called()
