# Handoff Brief: mac-dev-playbook Remediation & Storage-Aware Inference Provisioning

**Executor:** Claude Code (Sonnet-class), operating in the local clone of `mac-dev-playbook`.
**Runtime tier:** no LLM at runtime — everything this brief produces is deterministic (Ansible, shell, Python).
**Relationship to fleet briefs:** subordinate to Brief 1 (LiteLLM router). This repo provisions the Mac endpoints that Brief 1's router fronts. Nothing in this brief modifies the router itself.
**Repo:** `mac-dev-playbook` (Ansible playbook for personal / work / headless Mac provisioning).

## Standing Rules for All Executing Models (verbatim; these apply to this brief)

1. Sensitive data (journals, medical, mail, personal media) may only be processed by local aliases; work-owned Macs are excluded from sensitive workloads until further notice.
2. Build and test against synthetic data; the human runs real-data backfills himself.
3. Do not relitigate FIXED decisions; flag concerns in a notes section instead.
4. Every service gets: docker-compose (or launchd plist), a one-paragraph runbook, and a rollback note.

**Execution addendum for Claude Code specifically:**
- Work on a branch (`remediation/brief-mdp`), one commit per task, commit messages referencing the task ID below (e.g., `T2: flip Ollama bind default to loopback`).
- Never run `ansible-playbook` against the live machine without `--check` first. Full (non-check) runs are the human's call at review time.
- Do not pull models, start services, or modify launchd state during development. The model-pull planner must have a `--dry-run` mode, and dry-run is the only mode you execute.
- Do not push. The human reviews the branch and pushes.

---

## Context (findings this brief remediates)

A review of the repo (July 2026) found:

1. **Profile separation is broken.** All three inventory groups (`personal`, `work`, `headless`) contain the same host `localhost`. `--limit` restricts hosts, not variable resolution, so group_vars for all three groups merge alphabetically and `work.yml`'s empty `profile_*` lists override `personal.yml` and `headless.yml` on every run. Profile-specific packages are silently never installed.
2. **Rosetta install runs unprivileged.** `softwareupdate --install-rosetta` requires root; the play is `become: false` with no per-task override.
3. **`vars/apps.yml` is dead code** — loaded by nothing, already drifted from `group_vars/all.yml`.
4. **`failed_when: false` on every install task** makes the play unfalsifiable; headless automation cannot detect a failed provision.
5. **Ollama template defaults to `OLLAMA_HOST=0.0.0.0:11434`** even when the var is undefined — any host that runs the ollama task gets an unauthenticated inference API on all interfaces. Trigger condition is `'ollama' in homebrew_packages`, and `ollama` is in core, so this includes work Macs.
6. **launchd hygiene:** deprecated `launchctl load/unload`; logs in `/tmp`; plist is re-templated and the service bounced on every run (drops loaded models).
7. **README drift:** advertises `dotfiles`, `extra-packages`, `osx` tags that have no corresponding tasks.
8. **No inference-role concept.** The playbook models machines by profile; Brief 1 cares about inference role (`small` node = M1 Pro, `medium` node = M4 Pro). One flat `ollama_models` list on `headless` mixes 7B–32B models with no mapping to router aliases and no awareness of disk capacity.

## FIXED Decisions (do not relitigate; concerns → Notes section of your PR summary)

- **F1 — Inventory fix is distinct host aliases**, one per profile group (`mac-personal`, `mac-work`, `mac-headless`), each `ansible_connection=local` by default. `--limit` becomes mandatory and the README says so prominently. Not `ansible_group_priority`, not extra-vars profile switching.
- **F2 — Bind default is loopback.** The Ollama plist template defaults to `127.0.0.1:11434`. Non-loopback binds are set only by explicit per-host/role vars. Preferred non-loopback bind is the host's Tailscale interface IP, not `0.0.0.0` (LiteLLM on Unraid reaches Mac backends over the tailnet).
- **F3 — Work profile never gets inference services.** The Ollama configure include is guarded with `'work' not in group_names` in addition to any package-list condition. `ollama` and `lm-studio` move out of core into `personal` + `headless` profile lists. (Rationale: an MDM-managed employer machine must not serve or listen on the home LAN beyond what the work VLAN already permits.)
- **F4 — Inference roles are orthogonal to profiles.** New inventory groups `inference_small` and `inference_medium` (role groups). A host may be in one profile group and at most one inference-role group. Inference configuration (bind address, keep-alive, model manifest selection) keys off role-group membership, never off "ollama happens to be installed."
- **F5 — Model selection is manifest-driven and storage-aware.** A single `model_manifest.yml` is the source of truth for models: per-entry `name`, `engine`, `size_gb` (declared, not queried at runtime), `role` (`small`/`medium`/either), and `priority` (integer, lower pulls first). A deterministic planner selects, in priority order, only models that fit within free space minus a reserve floor. **The planner never deletes models** — evictions are reported as recommendations for the human. Deterministic-first: sizes come from the manifest; post-pull verification reconciles declared vs. actual.
- **F6 — Engine scope for v1 is Ollama.** The manifest schema carries an `engine` field so llama.cpp / LM Studio / MLX entries can be added later, but v1 implements pull/verify for `engine: ollama` only. Other engines present in the manifest are skipped with a logged notice, not an error.
- **F7 — Services keep residency.** Inference-role plists set `OLLAMA_KEEP_ALIVE=-1` (G19 latency requirement: model stays resident). Plist changes go through a handler so the service is only bounced when the rendered plist actually changed.
- **F8 — The playbook emits, never edits, router config.** After configuring an inference node, template a LiteLLM `model_list` snippet (alias → this host's tailnet name + port) into `artifacts/litellm/` (gitignored). The human applies it on Unraid. No task ever reaches across to Unraid.
- **F9 — Failures must fail.** Per-item registration stays for diagnostics, but the play ends with an `assert` that failed taps/packages/casks/MAS lists are empty. An opt-out `-e soft_fail=true` preserves the old interactive behavior; the default is strict.

## Decision Gates

Gate numbering continues the fleet-wide register; the human confirms the next free number at review (last known allocated: G24). Provisional IDs below.

| Gate | Question | Owner / default |
|---|---|---|
| **G25** | **Model manifest contents.** The preferred-model list (names, sizes, role mapping, priorities) is not yet provided. | Human provides. Executor ships the schema, the planner, and a clearly-marked SYNTHETIC example manifest (Standing Rule 2) used by all tests. |
| **G26** | **Disk reserve floor.** How much free space must remain after pulls? | Default `reserve_floor_gb: 100` on inference nodes (Time Machine local snapshots, Xcode, and caches eat headroom fast). Overridable per host. |
| **G27** | **Repo origin.** Dual-deployment IP rule says personal tooling lives in personal Gitea; repo is currently GitHub-origin and public. | Recommendation: Gitea as origin, GitHub as push mirror. Human decision; executor does nothing here but leaves a TODO in the README. |
| **G28** | **Core list pruning.** `docker` + `colima` + `orbstack` is three container runtimes in core. | Human picks one story per profile at review; executor flags but does not remove beyond F3's moves. |

---

## Phase 1 — Correctness (branch commits T1–T4)

**T1 — Inventory rewrite per F1.** Distinct aliases, keep the `profiles:children` parent, keep the remote-headless commented example. Update README: `--limit` is mandatory, with the three canonical invocations.
**T2 — Rosetta privilege fix.** `become: true` on the `softwareupdate --install-rosetta` task only.
**T3 — Delete `vars/apps.yml`.** Confirm via `grep -r "vars/apps" .` that nothing references it.
**T4 — README reconciliation.** Tags section lists only tags that exist (`homebrew`, `mas`, `config`, `ollama`, plus any this brief adds). Remove or implement `dotfiles`/`extra-packages`/`osx` — removal is the default; do not invent new task content for them.

**Acceptance (Phase 1):** `ansible-playbook main.yml --limit personal --check` resolves `profile_homebrew_packages` to personal's list (verify via the pre_task debug output, which should now show core+personal counts); `ansible-lint` (see T12) reports no errors on changed files; repo contains no references to the deleted vars file.
**Rollback:** each task is one commit; `git revert` restores prior behavior. Inventory rollback note: the old single-`localhost` inventory is preserved in the commit history and in `docs/rollback-notes.md`.

## Phase 2 — Hardening (T5–T8)

**T5 — Bind default flip per F2.** Template default `127.0.0.1:11434`; add `ollama_bind` var documentation; role-group vars set the tailnet bind for inference nodes. Include a comment in the template explaining why `0.0.0.0` is not the default (unauthenticated API; VLAN enforcement is the backstop, not the control).
**T6 — Work guard per F3.** Guard the ollama include; move `ollama` and `lm-studio` from `core_homebrew_*` into `personal` and `headless` profile lists.
**T7 — launchd hygiene per F7.** `launchctl bootstrap gui/$UID` / `bootout` replacing load/unload; logs to `~/Library/Logs/ollama/`; label `com.ollama.serve`; plist templated with `notify: restart ollama` handler; `KeepAlive` retained; add `OLLAMA_KEEP_ALIVE=-1` for inference roles. Add a `pmset`-based never-sleep-on-AC task, tagged `power`, applied to inference-role hosts only (requires `become: true`; document in runbook).
**T8 — Strict failure handling per F9.** Bulk-install first (`community.general.homebrew` with the full list), fall back to a per-item retry loop for diagnostics on failure, end with the assert. `soft_fail` escape hatch.

**Acceptance (Phase 2):** rendered plist (from `--check --diff`) shows loopback default on a host with no role group and tailnet bind on a host in `inference_small`; a run with `mac-work` in `--limit` produces zero ollama tasks; deliberately adding a bogus package name makes the play exit non-zero (and exit zero with `soft_fail=true`).
**Rollback:** per-commit revert; `docs/rollback-notes.md` gains a paragraph per task, including how to restore the old plist (`bootout` new label, `bootstrap` old plist from git history).

## Phase 3 — Inference roles & storage-aware model system (T9–T11)

**T9 — Role groups per F4.** Inventory gains `[inference_small]` / `[inference_medium]` (empty by default, with commented examples showing `mac-personal` joining a role). `group_vars/inference_small.yml` and `inference_medium.yml` carry: bind var, keep-alive, `model_roles: [small]` / `[medium]`, `reserve_floor_gb`.

**T10 — Manifest + planner per F5/F6.** Deliverables:
- `model_manifest.yml` — SYNTHETIC example entries only, each field commented, header stating "SYNTHETIC — replace via G25." Schema: `{name, engine, size_gb, role, priority, notes}`.
- `scripts/plan_model_pulls.py` (stdlib only, no pip deps): reads manifest + role filter + models dir; free space via `os.statvfs` on the volume containing the models dir; queries `ollama list` for present models (present models cost zero and are kept); greedy selection by priority of entries whose `size_gb` fits within `free − reserve_floor_gb`; outputs a human-readable plan and a JSON plan; `--dry-run` prints only; non-ollama engines listed under "skipped (engine not implemented)"; models that don't fit listed under "deferred (insufficient space)" with the shortfall; **never emits a delete action** — if a higher-priority model can't fit but lower-priority models are present, it prints an eviction *recommendation* only.
- `tasks/pull-models.yml` — runs the planner in dry-run, shows the plan, then executes pulls from the JSON plan (async, per existing 30-min pattern), then a verification step comparing actual on-disk size to declared `size_gb` and warning on >20% divergence (feeds manifest corrections back to the human).

**T11 — Router artifact per F8.** `templates/litellm-snippet.yml.j2` rendering each configured backend as a `model_list` entry (alias from role, `api_base` = `http://<tailnet-name>:11434/v1`), written to `artifacts/litellm/<hostname>.yml`; `artifacts/` gitignored.

**Acceptance (Phase 3):** planner unit-tested against synthetic manifests covering: everything fits; nothing fits; partial fit honoring priority; already-present model excluded from cost; non-ollama engine skipped; reserve floor respected exactly at the boundary. Tests run with a fake `ollama list` (stub via env var or injected command path — the executor must not require ollama installed to test). `tasks/pull-models.yml` passes `--check`. The rendered LiteLLM snippet for a synthetic `inference_small` host matches the alias taxonomy.
**Rollback:** the model system is additive — reverting T9–T11 commits restores Phase 2 state; pulled models are never deleted by any code path, so there is nothing destructive to roll back.

## Phase 4 — CI, runbooks, docs (T12–T13)

**T12 — Lint.** `ansible-lint` config + GitHub Action running `ansible-lint` and the planner's unit tests on PR. Python tests via `unittest` (no new deps).
**T13 — Runbooks per Standing Rule 4.** `runbooks/ollama.md` (what it is, where it binds and why, restart via bootstrap/bootout, log location, rollback) and `runbooks/model-management.md` (manifest schema, how to run the planner, how eviction recommendations are handled — by the human). One paragraph each plus commands; not essays.

**Acceptance (Phase 4):** CI green on the branch; both runbooks exist and are linked from the README.

## Overall acceptance for the brief

`--check` runs clean for all three profiles under the new inventory; work profile provably receives no inference configuration; a synthetic-manifest dry run on a machine with constrained free space produces a correct, priority-ordered, non-destructive plan; the play fails loudly on a broken package by default; every changed/added service has plist + runbook + rollback note.

## Notes (concerns register — append yours here, do not relitigate FIXED items)

- Homebrew formulas cannot be meaningfully version-pinned the way the LiteLLM container is digest-pinned; version drift on `brew upgrade` is accepted risk. Flag any package where that assumption feels wrong.
- `size_gb` in the manifest is declared, not discovered — a wrong declaration can over-fill the disk by the error margin. The reserve floor (G26) is the buffer; the post-pull verification step is the correction loop.
- The three-host-alias inventory means `ansible all -m ping` reports three hosts on one machine; cosmetic, documented in README.
