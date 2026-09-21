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

    def test_whisper_and_mlx_still_skipped(self):
        # T14 implements llamacpp; whisper/mlx_hf remain not-implemented.
        models = [M("whisper:1", "whisper", 1, "small", 1),
                  M("mlxhf:1", "mlx_hf", 1, "small", 1),
                  M("ok:1", "ollama", 5, "small", 1)]
        plan = planner.build_plan(models, "small",
                                  free_gb=500, reserve_floor_gb=0, present={})
        skipped = names(plan["skipped"])
        self.assertIn("whisper:1", skipped)
        self.assertIn("mlxhf:1", skipped)
        self.assertNotIn("ok:1", skipped)


def LC(name, size, role, priority, port, hf_repo="org/repo", quant_file=None):
    return {"name": name, "engine": "llamacpp", "size_gb": size, "role": role,
            "priority": priority, "port": port, "hf_repo": hf_repo,
            "quant_file": quant_file or (name.replace(":", "_") + ".gguf")}


class TestLlamacpp(unittest.TestCase):

    def test_llamacpp_is_a_real_pull_candidate(self):
        # T14: llamacpp competes for budget alongside ollama and carries its
        # engine-specific fields into the plan.
        models = [LC("cpp:1", 5, "medium", 1, 8091),
                  M("oll:1", "ollama", 5, "medium", 2),
                  M("whisper:1", "whisper", 1, "medium", 1)]
        plan = planner.build_plan(models, "medium",
                                  free_gb=200, reserve_floor_gb=100, present={})
        pulled = {e["name"]: e for e in plan["pull"]}
        self.assertIn("cpp:1", pulled)
        self.assertEqual(pulled["cpp:1"]["engine"], "llamacpp")
        self.assertEqual(pulled["cpp:1"]["port"], 8091)
        self.assertEqual(pulled["cpp:1"]["hf_repo"], "org/repo")
        self.assertIn("quant_file", pulled["cpp:1"])
        # both engines competed for the same budget
        self.assertIn("oll:1", pulled)
        self.assertNotIn("cpp:1", names(plan["skipped"]))

    def test_llamacpp_present_by_file(self):
        with tempfile.TemporaryDirectory() as d:
            quant = "model.Q4_K_M.gguf"
            with open(os.path.join(d, quant), "wb") as fh:
                fh.write(b"x" * 1024)  # tiny stand-in file
            models = [LC("cpp:1", 5, "medium", 1, 8091, quant_file=quant)]
            present = planner.llamacpp_present(models, d)
            self.assertIn("cpp:1", present)
            # and build_plan keeps a present model at zero cost
            plan = planner.build_plan(models, "medium",
                                      free_gb=101, reserve_floor_gb=100, present=present)
            self.assertIn("cpp:1", names(plan["present"]))
            self.assertEqual(plan["pull"], [])

    def test_port_collision_warning(self):
        models = [LC("a:1", 1, "medium", 1, 8091),
                  LC("b:1", 1, "medium", 2, 8091)]  # same port
        plan = planner.build_plan(models, "medium",
                                  free_gb=200, reserve_floor_gb=0, present={})
        self.assertTrue(any("collision" in w for w in plan["warnings"]))

    def test_port_registry_mismatch_warning(self):
        models = [LC("a:1", 1, "medium", 1, 8091)]
        plan = planner.build_plan(models, "medium", free_gb=200,
                                  reserve_floor_gb=0, present={},
                                  ports={"a:1": 9999})
        self.assertTrue(any("registry" in w for w in plan["warnings"]))

    def test_load_ports_registry(self):
        content = ("ports:\n"
                   "  a:1: 8091\n"
                   "  b:1: 8092\n"
                   "models:\n"
                   '  - { name: "a:1", engine: llamacpp, size_gb: 1, role: medium,'
                   ' priority: 1, port: 8091, hf_repo: "o/r", quant_file: "a.gguf" }\n')
        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as fh:
            fh.write(content)
            path = fh.name
        try:
            ports = planner.load_ports(path)
            self.assertEqual(ports.get("a:1"), 8091)
            self.assertEqual(ports.get("b:1"), 8092)
            # and the models section still parses with llamacpp fields
            models = planner.load_manifest(path)
            self.assertEqual(models[0]["port"], 8091)
            self.assertEqual(models[0]["hf_repo"], "o/r")
        finally:
            os.unlink(path)

    def test_verify_covers_llamacpp(self):
        models = [LC("cpp:1", 5, "medium", 1, 8091)]
        result = planner.build_verification(models, {"cpp:1": 8.0})  # 60% over
        self.assertEqual(len(result["diverged"]), 1)
        self.assertEqual(result["diverged"][0]["name"], "cpp:1")

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


class TestServesAliasLint(unittest.TestCase):

    def test_clean_manifest_has_no_violations(self):
        models = [M("a:1", "ollama", 5, "small", 1), M("b:1", "ollama", 5, "medium", 1)]
        models[0]["serves_alias"] = "small"
        models[1]["serves_alias"] = "medium"
        self.assertEqual(planner.lint_manifest(models), [])

    def test_two_claimants_same_alias_and_role_is_a_violation(self):
        models = [M("a:1", "ollama", 5, "small", 1), M("b:1", "ollama", 5, "small", 2)]
        models[0]["serves_alias"] = "small"
        models[1]["serves_alias"] = "small"
        violations = planner.lint_manifest(models)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0]["alias"], "small")
        self.assertEqual(set(violations[0]["entries"]), {"a:1", "b:1"})

    def test_same_alias_different_roles_is_allowed(self):
        # per-role uniqueness: 'embed' could legitimately exist once per role
        models = [M("a:1", "ollama", 5, "small", 1), M("b:1", "ollama", 5, "medium", 1)]
        models[0]["serves_alias"] = "embed"
        models[1]["serves_alias"] = "embed"
        self.assertEqual(planner.lint_manifest(models), [])

    def test_shipped_manifest_lints_clean(self):
        self.assertEqual(planner.lint_manifest(planner.load_manifest(_MANIFEST)), [])


class TestAlsoServes(unittest.TestCase):
    """Schema v1.3: one entry may publish extra bare aliases for the SAME
    backend and tag, so the router can declare a fallback without pretending
    the fallback is a different model (MDP-4 T-MDP4-9)."""

    def test_aliases_of_collects_both_fields(self):
        m = M("a:1", "ollama", 5, "small", 1)
        m["serves_alias"] = "small"
        m["also_serves"] = ["medium-degraded", "tiny"]
        self.assertEqual(planner.aliases_of(m), ["small", "medium-degraded", "tiny"])

    def test_aliases_of_handles_absent_and_empty(self):
        self.assertEqual(planner.aliases_of(M("a:1", "ollama", 5, "small", 1)), [])
        m = M("a:1", "ollama", 5, "small", 1)
        m["serves_alias"] = "small"
        m["also_serves"] = []
        self.assertEqual(planner.aliases_of(m), ["small"])

    def test_aliases_of_tolerates_a_bare_string(self):
        # `also_serves: medium-degraded` without brackets is a plausible typo;
        # treating it as the characters of a string would be silently absurd.
        m = M("a:1", "ollama", 5, "small", 1)
        m["also_serves"] = "medium-degraded"
        self.assertEqual(planner.aliases_of(m), ["medium-degraded"])

    def test_duplicate_across_serves_alias_and_also_serves_is_a_violation(self):
        # THE case this lint exists for: a collision between one entry's
        # also_serves and another entry's serves_alias renders two conflicting
        # bare model_name blocks, exactly like two serves_alias claimants.
        a = M("a:1", "ollama", 5, "small", 1)
        b = M("b:1", "ollama", 5, "small", 2)
        a["also_serves"] = ["medium-degraded"]
        b["serves_alias"] = "medium-degraded"
        violations = planner.lint_manifest([a, b])
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0]["alias"], "medium-degraded")
        self.assertEqual(set(violations[0]["entries"]), {"a:1", "b:1"})

    def test_duplicate_across_two_also_serves_is_a_violation(self):
        a = M("a:1", "ollama", 5, "small", 1)
        b = M("b:1", "ollama", 5, "small", 2)
        a["also_serves"] = ["degraded"]
        b["also_serves"] = ["degraded"]
        self.assertEqual(len(planner.lint_manifest([a, b])), 1)

    def test_same_also_serves_in_different_roles_is_allowed(self):
        a = M("a:1", "ollama", 5, "small", 1)
        b = M("b:1", "ollama", 5, "medium", 1)
        a["also_serves"] = ["degraded"]
        b["also_serves"] = ["degraded"]
        self.assertEqual(planner.lint_manifest([a, b]), [])

    def test_cloud_alias_is_rejected_like_a_cloud_tag(self):
        # The bare alias is the name a human types at the router. A -cloud
        # alias routes inference off-LAN just as effectively as a -cloud tag.
        m = M("a:1", "ollama", 5, "small", 1)
        m["also_serves"] = ["small-cloud"]
        hits = planner.lint_cloud_tags([m])
        self.assertEqual(len(hits), 1)
        self.assertIn("small-cloud", hits[0])

    def test_self_claimed_alias_is_flagged(self):
        m = M("a:1", "ollama", 5, "small", 1)
        m["serves_alias"] = "small"
        m["also_serves"] = ["small"]
        self.assertEqual(planner.lint_self_claimed_alias([m]),
                         [{"name": "a:1", "alias": "small"}])

    def test_shipped_manifest_publishes_medium_degraded_from_small(self):
        models = planner.load_manifest(_MANIFEST)
        by_alias = {}
        for m in models:
            for a in planner.aliases_of(m):
                by_alias.setdefault((a, m.get("role")), []).append(m["name"])
        self.assertEqual(by_alias[("medium-degraded", "small")], ["qwen3.5:9b-mlx"])
        # F-MDP4-7: the mechanism ships, the model does NOT change.
        self.assertEqual(by_alias[("small", "small")], ["qwen3.5:9b-mlx"])

    def test_lint_cli_fails_on_a_duplicate_alias(self):
        # End to end through the CLI, since that is what litellm-artifact.yml
        # actually runs before rendering the snippet.
        body = (
            "models:\n"
            '  - { name: "a:1", engine: ollama, size_gb: 1, role: small, '
            'priority: 1, serves_alias: small, also_serves: [degraded] }\n'
            '  - { name: "b:1", engine: ollama, size_gb: 1, role: small, '
            'priority: 2, serves_alias: degraded }\n'
        )
        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as fh:
            fh.write(body)
            path = fh.name
        try:
            r = subprocess.run([sys.executable, _SCRIPT, "--manifest", path, "--lint"],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 1, r.stderr)
            self.assertIn("degraded", r.stderr)
        finally:
            os.unlink(path)


class TestFlowSequenceParsing(unittest.TestCase):
    """The minimal YAML parser has to survive the new field. A flow list
    contains commas, and the flow-mapping splitter splits on commas."""

    def _one(self, body):
        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as fh:
            fh.write("models:\n" + body)
            path = fh.name
        try:
            return planner.load_manifest(path)[0]
        finally:
            os.unlink(path)

    def test_multi_item_flow_list_does_not_split_the_entry(self):
        m = self._one('  - { name: "a:1", engine: ollama, '
                      'also_serves: [one, two, three], role: small }\n')
        self.assertEqual(m["also_serves"], ["one", "two", "three"])
        # and the keys AFTER the list are still parsed, which is what a naive
        # comma split would destroy
        self.assertEqual(m["role"], "small")
        self.assertEqual(m["name"], "a:1")

    def test_empty_flow_list(self):
        m = self._one('  - { name: "a:1", also_serves: [], role: small }\n')
        self.assertEqual(m["also_serves"], [])
        self.assertEqual(m["role"], "small")

    def test_block_style_entry_also_parses_the_list(self):
        m = self._one('  - name: "a:1"\n'
                      '    also_serves: [one, two]\n'
                      '    role: small\n')
        self.assertEqual(m["also_serves"], ["one", "two"])
        self.assertEqual(m["role"], "small")

    def test_quoted_commas_inside_notes_still_work(self):
        # regression guard for the pre-existing quote handling
        m = self._one('  - { name: "a:1", also_serves: [x], '
                      'notes: "commas, and colons: fine", role: small }\n')
        self.assertEqual(m["also_serves"], ["x"])
        self.assertEqual(m["notes"], "commas, and colons: fine")
        self.assertEqual(m["role"], "small")


class TestCloudTagLint(unittest.TestCase):
    """A `-cloud` tag proxies inference off-LAN and must never reach a pull."""

    def test_cloud_tag_is_rejected(self):
        models = small_models() + [M("qwen3.5:9b-cloud", "ollama", 0, "small", 1)]
        self.assertEqual(planner.lint_cloud_tags(models), ["qwen3.5:9b-cloud"])

    def test_clean_manifest_has_no_cloud_tags(self):
        self.assertEqual(planner.lint_cloud_tags(small_models()), [])

    def test_shipped_manifest_has_no_cloud_tags(self):
        self.assertEqual(
            planner.lint_cloud_tags(planner.load_manifest(_MANIFEST)), [])

    def test_lint_cli_fails_on_a_cloud_tag(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as fh:
            fh.write('models:\n'
                     '  - { name: "qwen3.5:9b-cloud", engine: ollama, size_gb: 1,'
                     ' role: small, priority: 1, tier: dev }\n')
            path = fh.name
        try:
            proc = subprocess.run(
                [sys.executable, _SCRIPT, "--manifest", path, "--lint"],
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("-cloud", proc.stderr)
        finally:
            os.unlink(path)


class TestTierLint(unittest.TestCase):
    """`tier` is the residency class; an unknown value would silently unpin."""

    def test_known_tiers_pass(self):
        models = small_models()
        models[0]["tier"] = "pipeline"
        models[1]["tier"] = "standby"
        self.assertEqual(planner.lint_tiers(models), [])

    def test_unknown_tier_is_reported(self):
        models = small_models()
        models[0]["tier"] = "production"
        self.assertEqual(planner.lint_tiers(models),
                         [{"name": "a:7b", "tier": "production"}])

    def test_absent_tier_is_allowed_and_plans_as_dev(self):
        self.assertEqual(planner.lint_tiers(small_models()), [])
        plan = planner.build_plan(small_models(), "small",
                                  free_gb=200, reserve_floor_gb=100, present={})
        self.assertTrue(all(e["tier"] == "dev" for e in plan["pull"]))

    def test_pipeline_tier_is_carried_into_the_plan(self):
        models = small_models()
        models[0]["tier"] = "pipeline"
        plan = planner.build_plan(models, "small",
                                  free_gb=200, reserve_floor_gb=100, present={})
        tiers = {e["name"]: e["tier"] for e in plan["pull"]}
        self.assertEqual(tiers["a:7b"], "pipeline")

    def test_shipped_manifest_pins_exactly_the_alias_models(self):
        models = planner.load_manifest(_MANIFEST)
        pinned = {m["name"] for m in models
                  if m.get("tier") == "pipeline" and m.get("engine") == "ollama"}
        self.assertEqual(pinned, {"qwen3.5:9b-mlx", "qwen3-embedding:4b",
                                  "qwen3.6:27b-mlx"})
        # Every pinned ollama entry must actually serve an alias, or it is
        # spending a slot for nothing.
        for m in models:
            if m.get("tier") == "pipeline":
                self.assertTrue(m.get("serves_alias"), m.get("name"))


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
