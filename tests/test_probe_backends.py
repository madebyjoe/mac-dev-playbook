#!/usr/bin/env python3
"""Unit tests for scripts/probe_backends.py (stdlib unittest, no deps).

Uses a stdlib http.server stub bound to 127.0.0.1 — no real network, no ollama,
no tailnet, CI-safe (MDP-2 T18).
"""

import importlib.util
import os
import shutil
import socket
import tempfile
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


def _t(host, port, service="ollama", addr="127.0.0.1", path="/api/tags",
       transport="lan"):
    suffix = "_TAILNET_IP" if transport == "tailnet" else "_LAN_IP"
    return {"host": host, "role": "small", "transport": transport, "addr": addr,
            "port": port, "service": service, "path": path,
            "env_key": "%s%s" % (host.upper(), suffix)}


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
        r = probe_backends.probe(_t("mac-a", self.port, addr=None), timeout=2)
        self.assertFalse(r["ok"])
        self.assertIn("MAC-A_LAN_IP", r["error"])

    def test_missing_tailnet_ip_names_the_tailnet_key(self):
        # Under `tailnet` the LOAD-BEARING key is *_TAILNET_IP, so that is the
        # key the failure must name — pointing at _LAN_IP would send the human
        # to fix the wrong line in .env (MDP-4).
        r = probe_backends.probe(
            _t("mac-a", self.port, addr=None, transport="tailnet"), timeout=2)
        self.assertFalse(r["ok"])
        self.assertIn("MAC-A_TAILNET_IP", r["error"])


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
        ports, unrouted = probe_backends.ports_for_role(self._models(), "small")
        got = {(p, s) for (p, s, _path) in ports}
        # exactly ollama + whisper for the M1 Pro; the exact set forbids any
        # retired embed port from creeping back in (F13 r3).
        self.assertEqual(got, {(11434, "ollama"), (8082, "transcribe")})
        self.assertEqual(unrouted, [])

    def test_dev_port_probed_only_when_role_has_dev_tier_entries(self):
        # No entry is marked `tier: dev`, so the second ollama instance is not
        # part of this deployment and must not be probed.
        ports, _ = probe_backends.ports_for_role(self._models(), "small", dev_port=11435)
        self.assertNotIn(11435, {p for (p, _s, _path) in ports})

        models = self._models() + [
            {"name": "zoo:1", "engine": "ollama", "role": "small",
             "port": None, "tier": "dev"},
        ]
        ports, _ = probe_backends.ports_for_role(models, "small", dev_port=11435)
        got = {(p, s) for (p, s, _path) in ports}
        self.assertEqual(got, {(11434, "ollama"), (11435, "ollama-dev"),
                               (8082, "transcribe")})

    def test_dev_port_absent_from_registry_is_never_probed(self):
        models = self._models() + [
            {"name": "zoo:1", "engine": "ollama", "role": "small",
             "port": None, "tier": "dev"},
        ]
        ports, _ = probe_backends.ports_for_role(models, "small", dev_port=None)
        self.assertEqual({p for (p, _s, _path) in ports}, {11434, 8082})

    def test_ports_for_medium_include_llamacpp(self):
        ports, unrouted = probe_backends.ports_for_role(self._models(), "medium")
        got = {(p, s) for (p, s, _path) in ports}
        self.assertEqual(got, {(11434, "ollama"), (8091, "llamacpp")})
        self.assertEqual(unrouted, [])

    def test_tailnet_probes_the_front_only_and_reports_the_rest_unrouted(self):
        # MDP-4 / N3: on a roaming host every service binds 127.0.0.1 and
        # `tailscale serve` fronts the ollama pipeline port alone. The whisper
        # and dev-zoo ports are UP but have no remote route, so probing them
        # would report a healthy service as down.
        models = self._models() + [
            {"name": "zoo:1", "engine": "ollama", "role": "small",
             "port": None, "tier": "dev"},
        ]
        ports, unrouted = probe_backends.ports_for_role(
            models, "small", dev_port=11435, transport="tailnet")
        self.assertEqual({(p, s) for (p, s, _path) in ports},
                         {(11434, "ollama-front")})
        self.assertEqual({(u[0], u[1]) for u in unrouted},
                         {(11435, "ollama-dev"), (8082, "transcribe")})

    def test_build_targets_maps_env_and_roles(self):
        env = {"MAC_A_LAN_IP": "192.0.2.10", "MAC_B_LAN_IP": "192.0.2.20"}
        roles = {"small": ["mac-a"], "medium": ["mac-b"]}
        targets, unrouted = probe_backends.build_targets(env, roles, self._models())
        small = [t for t in targets if t["host"] == "mac-a"]
        self.assertEqual({t["port"] for t in small}, {11434, 8082})
        self.assertTrue(all(t["addr"] == "192.0.2.10" for t in small))
        self.assertTrue(all(t["transport"] == "lan" for t in small))
        medium = [t for t in targets if t["host"] == "mac-b"]
        self.assertEqual({t["port"] for t in medium}, {11434, 8091})
        self.assertEqual(unrouted, [])

    def test_build_targets_tailnet_host_dials_the_tailnet_ip(self):
        # The `lan` fixture keeps using RFC 5737 space; the `tailnet` fixture
        # uses the CGNAT range Tailscale actually assigns (100.64.0.0/10).
        env = {"MAC_A_LAN_IP": "192.0.2.10", "MAC_A_TAILNET_IP": "100.64.0.10",
               "MAC_B_LAN_IP": "192.0.2.20"}
        roles = {"small": ["mac-a"], "medium": ["mac-b"]}
        targets, unrouted = probe_backends.build_targets(
            env, roles, self._models(), transports={"mac-a": "tailnet"})
        small = [t for t in targets if t["host"] == "mac-a"]
        self.assertEqual([(t["addr"], t["port"], t["service"]) for t in small],
                         [("100.64.0.10", 11434, "ollama-front")])
        self.assertEqual(small[0]["env_key"], "MAC_A_TAILNET_IP")
        # mac-b was not named in `transports`, so it keeps the `lan` default
        # and its LAN IP — transport is per-host, never fleet-wide (F-MDP4-1).
        medium = [t for t in targets if t["host"] == "mac-b"]
        self.assertTrue(all(t["addr"] == "192.0.2.20" for t in medium))
        self.assertEqual([(u["host"], u["port"]) for u in unrouted],
                         [("mac-a", 8082)])

    def test_build_targets_missing_env_key_leaves_addr_none(self):
        roles = {"small": ["mac-a"], "medium": []}
        targets, _ = probe_backends.build_targets({}, roles, self._models())
        self.assertTrue(all(t["addr"] is None for t in targets))


class TestTransportReading(unittest.TestCase):
    """host_vars/<host>.yml is where transport lives (F-MDP4-1: topology, not an
    address, so it is versioned rather than in .env)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write(self, host, body):
        with open(os.path.join(self.dir, "%s.yml" % host), "w") as fh:
            fh.write(body)

    def test_missing_file_is_the_lan_default(self):
        self.assertEqual(probe_backends.host_transport("mac-a", self.dir), "lan")

    def test_reads_tailnet(self):
        self._write("mac-a", "---\ninference_transport: tailnet\n")
        self.assertEqual(probe_backends.host_transport("mac-a", self.dir), "tailnet")

    def test_comments_and_quotes_do_not_confuse_it(self):
        self._write("mac-a", '---\n# inference_transport: lan  <- a comment\n'
                             'inference_transport: "tailnet"  # roams\n')
        self.assertEqual(probe_backends.host_transport("mac-a", self.dir), "tailnet")

    def test_an_unknown_value_falls_back_to_lan(self):
        # Fail SAFE: an unrecognised transport must not silently become
        # `tailnet` and send the probe at an address nothing serves.
        self._write("mac-a", "inference_transport: wireguard\n")
        self.assertEqual(probe_backends.host_transport("mac-a", self.dir), "lan")

    def test_load_transports_maps_every_host(self):
        self._write("mac-a", "inference_transport: tailnet\n")
        got = probe_backends.load_transports(["mac-a", "mac-b"], self.dir)
        self.assertEqual(got, {"mac-a": "tailnet", "mac-b": "lan"})


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
