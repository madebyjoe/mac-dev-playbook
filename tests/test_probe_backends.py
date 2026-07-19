#!/usr/bin/env python3
"""Unit tests for scripts/probe_backends.py (stdlib unittest, no deps).

Uses a stdlib http.server stub bound to 127.0.0.1 — no real network, no ollama,
no tailnet, CI-safe (MDP-2 T18).
"""

import importlib.util
import os
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MOD = os.path.join(_REPO, "scripts", "probe_backends.py")
_spec = importlib.util.spec_from_file_location("probe_backends", _MOD)
probe_backends = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe_backends)


class _StubHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


def _start_stub():
    srv = HTTPServer(("127.0.0.1", 0), _StubHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def _closed_port():
    """Return a port with nothing listening (bind then release)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _t(host, port, service="ollama", lan_ip="127.0.0.1", path="/api/tags"):
    return {"host": host, "role": "small", "lan_ip": lan_ip, "port": port,
            "service": service, "path": path, "env_key": "%s_LAN_IP" % host.upper()}


class TestProbe(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.srv, cls.port = _start_stub()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_up_backend_passes(self):
        r = probe_backends.probe(_t("mac-a", self.port), timeout=2)
        self.assertTrue(r["ok"])
        self.assertEqual(r["status"], 200)
        self.assertIsNotNone(r["latency_ms"])

    def test_down_backend_fails(self):
        r = probe_backends.probe(_t("mac-a", _closed_port()), timeout=2)
        self.assertFalse(r["ok"])
        self.assertIsNotNone(r["error"])

    def test_missing_lan_ip_is_a_failure(self):
        r = probe_backends.probe(_t("mac-a", self.port, lan_ip=None), timeout=2)
        self.assertFalse(r["ok"])
        self.assertIn("MAC-A_LAN_IP", r["error"])


class TestRunExpectations(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.srv, cls.port = _start_stub()
        cls.down = _closed_port()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_all_up_is_overall_pass(self):
        targets = [_t("mac-a", self.port), _t("mac-b", self.port)]
        _, ok = probe_backends.run(targets, timeout=2)
        self.assertTrue(ok)

    def test_one_down_is_overall_fail(self):
        targets = [_t("mac-a", self.port), _t("mac-b", self.down)]
        _, ok = probe_backends.run(targets, timeout=2)
        self.assertFalse(ok)

    def test_expect_down_satisfied(self):
        # mac-b is actually down and expected down -> overall pass
        targets = [_t("mac-a", self.port), _t("mac-b", self.down)]
        _, ok = probe_backends.run(targets, timeout=2, expect_down="mac-b")
        self.assertTrue(ok)

    def test_expect_down_violated(self):
        # mac-b is expected down but is actually UP -> overall fail (enforcement/state wrong)
        targets = [_t("mac-a", self.port), _t("mac-b", self.port)]
        _, ok = probe_backends.run(targets, timeout=2, expect_down="mac-b")
        self.assertFalse(ok)


class TestTargetDerivation(unittest.TestCase):

    def _models(self):
        return [
            {"name": "chat:1", "engine": "ollama", "role": "small", "port": None},
            {"name": "whisper-large-v3-turbo", "engine": "whisper", "role": "small", "port": 8082},
            {"name": "parakeet", "engine": "mlx_hf", "role": "small", "port": None},
            {"name": "chatm:1", "engine": "ollama", "role": "medium", "port": None},
            {"name": "cpp:1", "engine": "llamacpp", "role": "medium", "port": 8091},
        ]

    def test_ports_for_small_are_ollama_plus_whisper(self):
        ports = probe_backends.ports_for_role(self._models(), "small")
        got = {(p, s) for (p, s, _path) in ports}
        # exactly ollama + whisper for the M1 Pro; the exact set forbids any
        # retired embed port from creeping back in (F13 r3).
        self.assertEqual(got, {(11434, "ollama"), (8082, "transcribe")})

    def test_ports_for_medium_include_llamacpp(self):
        ports = probe_backends.ports_for_role(self._models(), "medium")
        got = {(p, s) for (p, s, _path) in ports}
        self.assertEqual(got, {(11434, "ollama"), (8091, "llamacpp")})

    def test_build_targets_maps_env_and_roles(self):
        env = {"MAC_A_LAN_IP": "192.0.2.10", "MAC_B_LAN_IP": "192.0.2.20"}
        roles = {"small": ["mac-a"], "medium": ["mac-b"]}
        targets = probe_backends.build_targets(env, roles, self._models())
        small = [t for t in targets if t["host"] == "mac-a"]
        self.assertEqual({t["port"] for t in small}, {11434, 8082})
        self.assertTrue(all(t["lan_ip"] == "192.0.2.10" for t in small))
        medium = [t for t in targets if t["host"] == "mac-b"]
        self.assertEqual({t["port"] for t in medium}, {11434, 8091})

    def test_build_targets_missing_env_key_leaves_lan_ip_none(self):
        roles = {"small": ["mac-a"], "medium": []}
        targets = probe_backends.build_targets({}, roles, self._models())
        self.assertTrue(all(t["lan_ip"] is None for t in targets))


class TestInventoryParsing(unittest.TestCase):

    def test_parse_inventory_roles(self):
        text = (
            "[headless]\nmac-headless ansible_connection=local\n\n"
            "[inference_small]\n# a comment\nmac-headless\n\n"
            "[inference_medium]\nmac-studio tailnet_ip=x\n\n"
            "[profiles:children]\npersonal\n"
        )
        roles = probe_backends.parse_inventory_roles(text)
        self.assertEqual(roles["small"], ["mac-headless"])
        self.assertEqual(roles["medium"], ["mac-studio"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
