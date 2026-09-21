#!/usr/bin/env python3
"""F-MDP4-3: no inference service may bind a wildcard address, on any host or
transport, with no override flag.

This is an INTEGRATION test — it drives the real assert in
`tasks/load-local-env.yml` through `ansible-playbook` — because the bug it
guards against is not in any Python function. The pattern is a regex embedded in
a Jinja expression inside a YAML scalar, and backslash handling differs by scalar
style: in a folded (`>-`) scalar a doubled backslash reaches the regex engine as
a LITERAL backslash, so `0\\.0\\.0\\.0` matches nothing and the guard silently
passes everything. That failure mode is invisible to review and to any unit test
of the surrounding code; only actually running the assert catches it.

Hermetic: points `local_env_path` at a nonexistent file so no `.env` is read, and
passes `allow_loopback=true` so the earlier F15 address assert (a different rule)
does not mask the one under test. Skipped where ansible-playbook is unavailable,
which is why CI's stdlib-only test job stays green.
"""

import os
import shutil
import subprocess
import tempfile
import unittest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ANSIBLE = shutil.which("ansible-playbook")

PLAYBOOK = """---
- hosts: all
  connection: local
  gather_facts: false
  become: false
  tasks:
    - ansible.builtin.import_tasks: {repo}/tasks/load-local-env.yml
""".format(repo=_REPO)


@unittest.skipUnless(_ANSIBLE, "ansible-playbook not installed")
class TestWildcardBindGuard(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.playbook = os.path.join(cls.dir, "guard.yml")
        with open(cls.playbook, "w") as fh:
            fh.write(PLAYBOOK)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def _run(self, host="headless", extra=()):
        cmd = [_ANSIBLE, self.playbook, "--limit", host,
               "-e", "local_env_path=/nonexistent/.env",
               "-e", "allow_loopback=true"]
        cmd += [a for pair in extra for a in ("-e", pair)]
        return subprocess.run(cmd, cwd=_REPO, capture_output=True, text=True)

    def test_legitimate_bind_passes(self):
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_every_wildcard_form_is_refused_and_named(self):
        # Each of these is a way to say "listen on every interface". A guard
        # that catches only the IPv4 spelling is not a guard.
        for bind in ("0.0.0.0", "0.0.0.0:11434", "::", "::11434", "[::]:11434"):
            with self.subTest(bind=bind):
                r = self._run(extra=("ollama_bind=%s" % bind,))
                self.assertNotEqual(r.returncode, 0,
                                    "%s was ACCEPTED as a bind" % bind)
                self.assertIn("Wildcard bind refused", r.stdout + r.stderr)
                # The message must name the offending value, or the operator is
                # told something is wrong but not which of three binds it was.
                # This is the assertion the folded-scalar bug actually broke:
                # `that` fired while the diagnostic list rendered empty.
                self.assertIn(bind.split(":")[0] if not bind.startswith("[") else "[::]",
                              r.stdout + r.stderr)

    def test_there_is_no_override_flag(self):
        # F-MDP4-3 is deliberately absolute. The flags that waive OTHER address
        # rules must not waive this one.
        for waiver in ("allow_loopback=true", "soft_fail=true",
                       "skip_residency_check=true"):
            with self.subTest(waiver=waiver):
                r = self._run(extra=("ollama_bind=0.0.0.0:11434", waiver))
                self.assertNotEqual(r.returncode, 0,
                                    "%s waived the wildcard guard" % waiver)

    def test_dev_zoo_bind_is_checked_too(self):
        # The dev instance is just as unauthenticated as the pipeline one.
        r = self._run(extra=("ollama_dev_bind=0.0.0.0:11435",))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Wildcard bind refused", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
