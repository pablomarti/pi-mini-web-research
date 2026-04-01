import importlib.util
import ipaddress
import os
from pathlib import Path
from unittest import TestCase, mock


MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "extensions" / "web-research" / "research.py"
)

SPEC = importlib.util.spec_from_file_location("research", MODULE_PATH)
research = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(research)


class SecurityValidationTests(TestCase):
    def test_blocks_loopback_ip(self):
        with self.assertRaises(ValueError):
            research._validate_target_url("http://127.0.0.1/test")

    def test_blocks_metadata_hostname(self):
        with mock.patch.object(
            research,
            "_resolve_ips_for_host",
            return_value=[ipaddress.ip_address("203.0.113.10")],
        ):
            with self.assertRaises(ValueError):
                research._validate_target_url("http://metadata.google.internal/compute")

    def test_blocks_embedded_credentials(self):
        with self.assertRaises(ValueError):
            research._validate_target_url("https://user:pass@example.com")

    def test_allows_allowlisted_hostname(self):
        with mock.patch.dict(
            os.environ, {"PI_WEB_RESEARCH_ALLOWLIST": "localhost"}, clear=False
        ):
            with mock.patch.object(
                research,
                "_resolve_ips_for_host",
                return_value=[ipaddress.ip_address("127.0.0.1")],
            ):
                research._validate_target_url("http://localhost/ok")

    def test_allows_allowlisted_cidr(self):
        with mock.patch.dict(
            os.environ, {"PI_WEB_RESEARCH_ALLOWLIST": "10.0.0.0/8"}, clear=False
        ):
            with mock.patch.object(
                research,
                "_resolve_ips_for_host",
                return_value=[ipaddress.ip_address("10.25.6.7")],
            ):
                research._validate_target_url("http://internal.example/resource")
