# Rollback notes

Every task in the remediation branch is a single commit, so the primary rollback
mechanism is `git revert <commit>` (or `git revert <oldest>..<newest>` for a
range). This file records anything a revert alone does not restore — chiefly
launchd/system state that lives outside the repo.

Find a task's commit with `git log --oneline --grep '^T<n>:'`.

## Phase 1 — Correctness

### T1 — Inventory rewrite
The old inventory pointed all three profile groups at a single `localhost`. It is
preserved in git history (the commit prior to T1). To roll back:
`git revert <T1>`. No machine state changes — the inventory is only read at
`ansible-playbook` time. Note that after reverting, `--limit` is no longer
enforced by distinct aliases and profile group_vars will again merge/override.

### T2–T4
Pure repo changes (task privilege, deleted dead vars file, README text). `git
revert` fully restores prior behaviour; no external state involved.

## Phase 2 — Hardening

### T5 — Bind default flip
Reverting `git revert <T5>` restores the template default of `0.0.0.0:11434` and
the removed `ollama_host`/`ollama_models` in `group_vars/headless.yml`. The
running service is not changed by the revert itself; the plist is only
re-rendered (and the service bounced) on the next `ansible-playbook` run when the
rendered content changes.

### T6 — Work guard
`git revert <T6>` re-adds `ollama`/`lm-studio` to core and removes the `'work'
not in group_names` guard. Nothing is uninstalled by the revert; to actually
remove ollama/lm-studio from a machine you would `brew uninstall` them by hand.

### T7 — launchd hygiene
This is the one task with external state. The new service uses label
`com.ollama.serve` (plist at `~/Library/LaunchAgents/com.ollama.serve.plist`),
logs under `~/Library/Logs/ollama/`, and is loaded with
`launchctl bootstrap gui/$UID`. To restore the **old** service after a
`git revert <T7>`:

```sh
uid=$(id -u)
# Tear down the new service and plist
launchctl bootout "gui/${uid}/com.ollama.serve" 2>/dev/null || true
rm -f ~/Library/LaunchAgents/com.ollama.serve.plist
# Re-render the old plist from the reverted template, then load it the old way
ansible-playbook main.yml --limit <profile> --tags ollama   # writes com.ollama.plist again
launchctl bootstrap "gui/${uid}" ~/Library/LaunchAgents/com.ollama.plist
```

(The pre-T7 template wrote label `com.ollama` to
`~/Library/LaunchAgents/com.ollama.plist` with logs in `/tmp`.) The `pmset -c
sleep 0` change applied to inference nodes is not auto-reverted; restore your
prior AC-sleep setting with `sudo pmset -c sleep <minutes>` if needed. The pmset
task requires a sudo password — pass `-K` when running against an inference node.

### T8 — Strict failure handling
`git revert <T8>` restores the old per-item `failed_when: false` install loops
and removes the final assert. No machine state is involved. If you need the old
non-failing behaviour without reverting, run with `-e soft_fail=true`.

## Phase 3 — Inference roles & storage-aware model system

The whole model system is **additive and non-destructive**, so rollback is
simply reverting commits — there is nothing to undo on disk.

### T9 — Inference role groups
`git revert <T9>` removes the `[inference_small]`/`[inference_medium]` groups and
their group_vars. The groups are empty by default, so no host loses
configuration unless you had added one to a role.

### T10 — Manifest, planner, pull task
`git revert <T10>` removes `model_manifest.yml`, `scripts/plan_model_pulls.py`,
`tasks/pull-models.yml`, and the tests. **No models are ever deleted by any code
path** — the planner only recommends evictions, and pulls are gated behind
`model_pull_dry_run=false`. Any models already pulled by the human stay on disk;
reverting the code does not touch them. To stop pulling without reverting, leave
`model_pull_dry_run` at its default (`true`).

### T11 — LiteLLM router artifact
`git revert <T11>` removes the template and the emit task. The generated files
live under `artifacts/` (gitignored) and are never applied by the playbook, so
reverting has no external effect; delete `artifacts/litellm/*.yml` by hand if you
want them gone. The router on Unraid is never touched by this repo.
