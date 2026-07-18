#!/usr/bin/env python3
"""Unit tests for scripts/plan_model_pulls.py (stdlib unittest, no deps).

Covers the acceptance scenarios from the brief: everything fits, nothing fits,
partial fit honouring priority, already-present excluded from cost, non-ollama
engine skipped, and the reserve floor respected exactly at the boundary. The
`ollama list` probe is stubbed via the OLLAMA_LIST_OUTPUT env var so ollama need
not be installed.
"""

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT = os.path.join(_REPO, "scripts", "plan_model_pulls.py")
_MANIFEST = os.path.join(_REPO, "model_manifest.yml")

_spec = importlib.util.spec_from_file_location("planner", _SCRIPT)
planner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(planner)


def M(name, engine, size, role, priority):
    return {"name": name, "engine": engine, "size_gb": size,
            "role": role, "priority": priority}


# A small synthetic model set reused across cases.
def small_models():
    return [
        M("a:7b", "ollama", 5, "small", 1),
        M("b:8b", "ollama", 8, "either", 2),
        M("mlx:7b", "mlx", 4, "either", 1),
        M("c:32b", "ollama", 20, "medium", 1),  # medium-only -> filtered for small
    ]


def names(entries):
    return [e["name"] for e in entries]


class TestPlanning(unittest.TestCase):

    def test_everything_fits(self):
        plan = planner.build_plan(small_models(), "small",
                                  free_gb=200, reserve_floor_gb=100, present={})
        self.assertEqual(names(plan["pull"]), ["a:7b", "b:8b"])
        self.assertEqual(plan["deferred"], [])
        self.assertEqual(names(plan["skipped"]), ["mlx:7b"])

    def test_nothing_fits(self):
        # budget = 0, so no ollama candidate fits.
        plan = planner.build_plan(small_models(), "small",
                                  free_gb=100, reserve_floor_gb=100, present={})
        self.assertEqual(plan["pull"], [])
        self.assertEqual(set(names(plan["deferred"])), {"a:7b", "b:8b"})
        for d in plan["deferred"]:
            self.assertIn("shortfall_gb", d)

    def test_partial_fit_honours_priority(self):
        # budget only fits one 5 GB model; the priority-1 model must win.
        models = [M("hi:1", "ollama", 5, "small", 1),
                  M("lo:1", "ollama", 5, "small", 2)]
        plan = planner.build_plan(models, "small",
                                  free_gb=105, reserve_floor_gb=100, present={})
        self.assertEqual(names(plan["pull"]), ["hi:1"])
        self.assertEqual(names(plan["deferred"]), ["lo:1"])

    def test_greedy_continues_past_a_nonfitting_higher_priority(self):
        # a priority-1 model that does not fit must not block a smaller
        # priority-2 model that does.
        models = [M("big:1", "ollama", 10, "small", 1),
                  M("small:1", "ollama", 3, "small", 2)]
        plan = planner.build_plan(models, "small",
                                  free_gb=105, reserve_floor_gb=100, present={})  # budget 5
        self.assertEqual(names(plan["pull"]), ["small:1"])
        self.assertEqual(names(plan["deferred"]), ["big:1"])

    def test_present_model_excluded_from_cost(self):
        present = {"a:7b": 4.9}
        plan = planner.build_plan(small_models(), "small",
                                  free_gb=106, reserve_floor_gb=100, present=present)
        self.assertNotIn("a:7b", names(plan["pull"]))
        self.assertIn("a:7b", names(plan["present"]))
        # a's 5 GB was NOT charged against the 6 GB budget, so b (8 GB) is still
        # deferred but the budget was never spent on a.
        self.assertEqual(plan["remaining_gb"], 6.0)

    def test_non_ollama_engine_skipped(self):
        plan = planner.build_plan(small_models(), "small",
                                  free_gb=500, reserve_floor_gb=0, present={})
        self.assertIn("mlx:7b", names(plan["skipped"]))
        self.assertNotIn("mlx:7b", names(plan["pull"]))

    def test_llamacpp_categorized_pending_t14(self):
        # F6.1: llamacpp is skipped but flagged as planned work (T14); other
        # non-ollama engines are flagged as not implemented.
        models = [M("cpp:1", "llamacpp", 5, "small", 1),
                  M("whisper:1", "whisper", 1, "small", 1),
                  M("ok:1", "ollama", 5, "small", 1)]
        plan = planner.build_plan(models, "small",
                                  free_gb=500, reserve_floor_gb=0, present={})
        by_name = {e["name"]: e for e in plan["skipped"]}
        self.assertEqual(by_name["cpp:1"]["status"], "pending_t14")
        self.assertEqual(by_name["whisper:1"]["status"], "not_implemented")
        self.assertNotIn("cpp:1", names(plan["pull"]))

    def test_reserve_floor_boundary_exact(self):
        # size exactly equals budget -> fits.
        models = [M("edge:1", "ollama", 5, "small", 1)]
        plan = planner.build_plan(models, "small",
                                  free_gb=105, reserve_floor_gb=100, present={})  # budget 5
        self.assertEqual(names(plan["pull"]), ["edge:1"])

    def test_reserve_floor_boundary_just_over(self):
        # one hundredth of a GB over budget -> deferred.
        models = [M("edge:1", "ollama", 5, "small", 1)]
        plan = planner.build_plan(models, "small",
                                  free_gb=104.99, reserve_floor_gb=100, present={})  # budget 4.99
        self.assertEqual(plan["pull"], [])
        self.assertEqual(names(plan["deferred"]), ["edge:1"])

    def test_eviction_recommendation_only(self):
        # A high-priority deferred model with a lower-priority present model
        # should produce a recommendation but never a delete action.
        models = [M("want:1", "ollama", 10, "small", 1),
                  M("old:1", "ollama", 8, "small", 5)]
        present = {"old:1": 8.0}
        plan = planner.build_plan(models, "small",
                                  free_gb=105, reserve_floor_gb=100, present=present)  # budget 5
        self.assertIn("want:1", names(plan["deferred"]))
        self.assertTrue(plan["eviction_recommendations"])
        rec = plan["eviction_recommendations"][0]
        self.assertEqual(rec["deferred"], "want:1")
        self.assertEqual(names(rec["evict_candidates"]), ["old:1"])
        # No delete/evict *action* anywhere in the machine plan.
        self.assertNotIn("delete", plan)
        self.assertNotIn("evict", plan)


class TestManifestParsing(unittest.TestCase):

    def test_parses_flow_style(self):
        # The ratified G25 manifest is written flow-style, with commas and colons
        # inside quoted notes.
        content = (
            "models:\n"
            '  - { name: "qwen3.5:9b-mlx", engine: ollama, size_gb: 8.0,'
            ' role: small, priority: 40, notes: "Primary alias, verify: 6.6GB" }\n'
            '  - { name: "whisper-large-v3-turbo", engine: whisper, size_gb: 1.6,'
            ' role: small, priority: 10, notes: "port 8082" }\n'
        )
        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as fh:
            fh.write(content)
            path = fh.name
        try:
            models = planner.load_manifest(path)
            self.assertEqual(len(models), 2)
            self.assertEqual(models[0]["name"], "qwen3.5:9b-mlx")  # colon in name
            self.assertEqual(models[0]["engine"], "ollama")
            self.assertEqual(models[0]["size_gb"], 8.0)
            self.assertEqual(models[0]["priority"], 40)
            # comma AND colon inside the quoted notes survived the split.
            self.assertEqual(models[0]["notes"], "Primary alias, verify: 6.6GB")
            self.assertEqual(models[1]["engine"], "whisper")
        finally:
            os.unlink(path)

    def test_shipped_manifest_parses(self):
        # Content-agnostic: the shipped manifest parses and every entry carries
        # the required schema fields. Does not assert specific models (they drift).
        models = planner.load_manifest(_MANIFEST)
        self.assertGreater(len(models), 0)
        for m in models:
            for field in ("name", "engine", "size_gb", "role", "priority"):
                self.assertIn(field, m, "%s missing %s" % (m.get("name"), field))

    def test_coerces_types(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as fh:
            fh.write("models:\n"
                     "  - name: x:1\n"
                     "    engine: ollama\n"
                     "    size_gb: 12\n"
                     "    role: either\n"
                     "    priority: 3\n"
                     '    notes: "hi: there"\n')  # colon inside quoted value
            path = fh.name
        try:
            models = planner.load_manifest(path)
            self.assertEqual(models[0]["size_gb"], 12)
            self.assertIsInstance(models[0]["size_gb"], int)
            self.assertEqual(models[0]["notes"], "hi: there")
        finally:
            os.unlink(path)


class TestOllamaListStub(unittest.TestCase):

    def test_parses_env_stub(self):
        stub = ("NAME                ID       SIZE      MODIFIED\n"
                "a:7b                abc      4.9 GB    2 days ago\n"
                "keep:1              def      512 MB    3 days ago\n")
        os.environ["OLLAMA_LIST_OUTPUT"] = stub
        try:
            present = planner.ollama_list()
        finally:
            del os.environ["OLLAMA_LIST_OUTPUT"]
        self.assertIn("a:7b", present)
        self.assertIn("keep:1", present)
        self.assertAlmostEqual(present["a:7b"], 4.9, places=2)

    def test_missing_ollama_returns_empty(self):
        present = planner.ollama_list(ollama_bin="definitely-not-a-real-binary-xyz")
        self.assertEqual(present, {})


class TestVerification(unittest.TestCase):

    def test_flags_large_divergence(self):
        models = [M("a:7b", "ollama", 5, "small", 1)]
        # actual 8 GB vs declared 5 GB -> 60% divergence, over the 20% tolerance.
        result = planner.build_verification(models, {"a:7b": 8.0})
        self.assertEqual(len(result["diverged"]), 1)
        self.assertEqual(result["diverged"][0]["name"], "a:7b")

    def test_ignores_small_divergence(self):
        models = [M("a:7b", "ollama", 5, "small", 1)]
        result = planner.build_verification(models, {"a:7b": 5.5})  # 10%
        self.assertEqual(result["diverged"], [])


class TestCli(unittest.TestCase):

    def test_cli_json_exit_zero(self):
        env = dict(os.environ, OLLAMA_LIST_OUTPUT="")
        proc = subprocess.run(
            [sys.executable, _SCRIPT, "--role", "small",
             "--free-gb", "110", "--reserve-floor-gb", "100",
             "--format", "json", "--dry-run"],
            capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('"pull"', proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
