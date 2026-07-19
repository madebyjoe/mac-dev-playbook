#!/usr/bin/env python3
"""Unit tests for scripts/local_env.py (stdlib unittest, no deps).

Synthetic env content only, written to a temp dir (Standing Rule 2 / MDP-2).
"""

import importlib.util
import os
import tempfile
import unittest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MOD = os.path.join(_REPO, "scripts", "local_env.py")
_spec = importlib.util.spec_from_file_location("local_env", _MOD)
local_env = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(local_env)


class TestParseEnv(unittest.TestCase):

    def test_basic_key_value(self):
        env = local_env.parse_env("MAC_HEADLESS_LAN_IP=192.0.2.10\n")
        self.assertEqual(env["MAC_HEADLESS_LAN_IP"], "192.0.2.10")

    def test_ignores_blanks_and_comments(self):
        env = local_env.parse_env("# a comment\n\n   \nX=y\n  # indented comment\n")
        self.assertEqual(env, {"X": "y"})

    def test_strips_whitespace_around_key_and_value(self):
        env = local_env.parse_env("  A =  1 \n")
        self.assertEqual(env["A"], "1")
        self.assertIn("A", env)

    def test_value_may_contain_equals(self):
        env = local_env.parse_env("P=a=b=c\n")  # split on the FIRST '=' only
        self.assertEqual(env["P"], "a=b=c")

    def test_tailnet_name_with_dots_survives(self):
        # Placeholder deliberately avoids a real tailnet suffix so this file stays
        # clean of the sanitization grep patterns (MDP-2 T15 scrub).
        env = local_env.parse_env("MAC_HEADLESS_TAILNET_NAME=mac-headless.tailnet-x.example\n")
        self.assertEqual(env["MAC_HEADLESS_TAILNET_NAME"], "mac-headless.tailnet-x.example")

    def test_line_without_equals_ignored(self):
        env = local_env.parse_env("garbage line\nA=1\n")
        self.assertEqual(env, {"A": "1"})


class TestLoadEnv(unittest.TestCase):

    def test_missing_file_returns_empty(self):
        self.assertEqual(local_env.load_env("/no/such/path/.env"), {})

    def test_load_from_tempdir(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".env")
            with open(path, "w") as fh:
                fh.write("# synthetic\nMAC_X_LAN_IP=192.0.2.5\nMAC_X_TAILNET_IP=198.51.100.5\n")
            env = local_env.load_env(path)
            self.assertEqual(env["MAC_X_LAN_IP"], "192.0.2.5")
            self.assertEqual(env["MAC_X_TAILNET_IP"], "198.51.100.5")


if __name__ == "__main__":
    unittest.main(verbosity=2)
