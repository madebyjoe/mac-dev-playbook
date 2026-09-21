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

## Phase 4 — CI, runbooks, docs

Pure additions with no machine state. `git revert <T12>` removes `.ansible-lint`
and the CI workflow (and would reintroduce the lint findings the commit fixed);
`git revert <T13>` removes the runbooks and their README links.

## MDP-2 — endpoint closure & local addressing

- **T15 (.env addressing):** additive. `git revert <T15>` removes the loader,
  `.env.example`, and the F15 assert; binds revert from `lan_ip` to the tailnet
  default. Your `.env` is gitignored and untouched. No machine state.
- **T16 (bare aliases):** `git revert <T16>` drops the bare-alias blocks and the
  `--lint` gate; snippets regress to role-namespaced-only. Artifacts are not live
  config, so the router keeps whatever you last applied.
- **T17 (transcribe service):** the one external state is the launchd service.
  To remove it: `launchctl bootout "gui/$(id -u)/com.inference.transcribe"` and
  delete `~/Library/LaunchAgents/com.inference.transcribe.plist`, then
  `git revert <T17>`. The downloaded GGML model in `~/.cache/whisper/models/` is
  never deleted by any code path. `embed` needs no rollback — it is just the
  ollama instance.

## M1 Pro remediation (2026-09-12) — transcribe path + model-slot thrashing

Two faults, both diagnosed against the live router and fixed here so they survive
a reboot and a rebuild. Full write-ups: `runbooks/ollama.md` and
`runbooks/transcribe.md`.

**Repo-only parts** (revert restores prior behaviour, no machine state): the
`tier` field and `ports.ollama_dev` in `model_manifest.yml`, the planner's
`-cloud`/tier lints, the slot-budget and dev-instance group_vars, the `OLLAMA_HOST`
environment on the pull tasks, the `--threads`/ffmpeg additions to the transcribe
role, and the router-snippet dev-namespace routing. Reverting the snippet change
does not touch the router — `artifacts/` is never applied by this repo.

**External state.** Three things outlive a `git revert`:

1. **New launchd labels.** `com.ollama.dev` (:11435) and `com.ollama.warm`
   (periodic warm-keeper). Remove both:

   ```sh
   uid=$(id -u)
   for l in com.ollama.warm com.ollama.dev; do
     launchctl bootout "gui/${uid}/$l" 2>/dev/null || true
     rm -f ~/Library/LaunchAgents/$l.plist
   done
   rm -f ~/.cache/mac-dev-playbook/ollama-warm.sh
   ```

   Setting `ollama_dev_enabled: false` achieves the same on the next run (the task
   boots out the dev instance and deletes its plist). **Before doing either,
   re-read why the split exists** — a single instance means the `dev/*` zoo shares
   the pipeline's slot budget again, which is the original fault.

2. **Disabled legacy plists.** The transcribe role now takes over hand-rolled
   whisper LaunchAgents the same way the ollama role already did: any
   `~/Library/LaunchAgents/*.plist` mentioning whisper or serving `/inference`
   (except `com.inference.transcribe.plist`) is booted out and renamed to
   `*.plist.disabled-by-playbook`. **Nothing is deleted.** To restore one, rename
   it back and `bootstrap` it — booting out `com.inference.transcribe` first if you
   want the old service to own `:8082`.

3. **Router config.** Repointing `dev/*` entries at `:11435` is a human edit on
   Unraid; this repo only emits the snippet. If you revert the split, those entries
   must go back to `:11434` by hand or they will 404.

No models are deleted by any path here, and the residency assertion is a read of
`/api/ps` — bypass it with `-e skip_residency_check=true` if it is in your way.

## Phase 3b — llama.cpp engine (T14, Amendment 1)

Additive and non-destructive, like the rest of the model system. `git revert`
of the T14 commits removes the llamacpp planner support, the
`templates/llamacpp-server.plist.j2` template, `tasks/configure-llamacpp.yml`,
the pull dispatch, and the router extension; after reverting, llamacpp entries go
back to being skipped-with-notice. The one external state is the per-model
launchd services: to remove one, `launchctl bootout "gui/$(id -u)/com.llamacpp.<safe-name>"`
and delete `~/Library/LaunchAgents/com.llamacpp.<safe-name>.plist`. **Downloaded
GGUFs in `~/.cache/llamacpp/models/` are never deleted by any code path** — remove
them by hand if you want the space back.

## MDP-4 — roaming `medium` transport (G30 r4)

**Repo-only parts** (revert restores prior behaviour, no machine state): the
`inference_transport` / `inference_bind_ip` vars and the transport-aware asserts
in `tasks/load-local-env.yml`, the bind-site substitutions in the group_vars and
plist templates, the transport branch in `templates/litellm-snippet.yml.j2`, the
transport handling in `scripts/probe_backends.py`, `ports.ollama_front` in the
manifest, and the new task/template/runbook files. `artifacts/` is never applied
by this repo, so re-rendering a snippet does not touch the router.

**The cheap revert.** `inference_transport` defaults to `lan` in
`group_vars/all.yml`, so **deleting `host_vars/mac-personal.yml` alone** puts the
host back on the LAN bind:

```sh
rm host_vars/mac-personal.yml
ansible-playbook main.yml --limit personal --tags config,ollama
```

That does NOT remove the front or the tailnet door — they just stop being used.
To remove them properly, in this order:

```sh
# 1. Close the tailnet door FIRST. Leaving it open while the front goes away
#    means the router connects and hangs on a dead port instead of failing fast.
tailscale serve --tcp=11434 off

# 2. Tear down the Caddy front.
uid=$(id -u)
launchctl bootout "gui/${uid}/com.madebyjoe.inference-front" 2>/dev/null || true
rm -f ~/Library/LaunchAgents/com.madebyjoe.inference-front.plist
rm -rf ~/.config/inference-front

# 3. Return the host to the LAN bind.
rm host_vars/mac-personal.yml
ansible-playbook main.yml --limit personal --tags config,ollama
```

**External state that outlives a `git revert`:**

1. **The tailnet serve config.** Held by `tailscaled`, not by any file in this
   repo, and it **survives a reboot and a revert**. `tailscale serve --tcp=11434 off`
   is the only thing that removes it. Check with `tailscale serve status`.
2. **The `com.madebyjoe.inference-front` launchd agent.** Booting it out is not
   enough on its own — with the plist still in `~/Library/LaunchAgents` it comes
   back at next login. Delete the plist too.
3. **Homebrew `caddy`.** Installed by the front task, never removed by any code
   path. `brew uninstall caddy` if you want the space back.
4. **Router config (human).** The `medium` `api_base` on Unraid points at the
   tailnet address. Reverting here does not change the router — repoint it to the
   LAN address by hand, or `medium` stays down. Same for the
   `fallbacks: {medium: [medium-degraded]}` entry.
5. **The tailnet ACL (human).** The `tag:llm-router → tag:inference-roaming:11434`
   rule is the access control for this path. Remove it when the path goes away,
   or it silently permits more than the topology needs.

**Order matters on the way IN, too.** Applying Phase B moves `medium` off the LAN
entirely. Until the router side is done (H1 tailnet node, H2 ACL, H3 repointed
`api_base`), the router **cannot reach `medium` at all** — the alias 503s. That is
expected and is what `fallbacks: {medium: [medium-degraded]}` is for; it is not a
regression to debug.

No models are deleted by any path here, and the `0.0.0.0` prohibition (F-MDP4-3)
has no override flag by design — if a revert leaves you wanting one, the answer is
`inference_transport`, not a wildcard bind.

### MDP-4 Phase C — `small` uplift mechanism

Mechanism only; **no model or alias changed** (F-MDP4-7). `serves_alias: small`
still points at `qwen3.5:9b-mlx`.

- **`also_serves` (manifest v1.3)** is repo-only. Reverting drops the extra bare
  `model_name` blocks from the snippet; the router keeps whatever you last
  applied, since `artifacts/` is never applied by this repo. If you revert while
  the router still declares `fallbacks: {medium: [medium-degraded]}`, remove that
  fallback too or the router names an alias nothing defines.
- **`gpu_wired_limit_mb`** is undefined by default and the task is not even
  imported then — a complete no-op on every host. Where it HAS been set, two
  pieces of external state outlive a revert:
  ```sh
  sudo launchctl bootout system/com.madebyjoe.gpu-wired-limit 2>/dev/null || true
  sudo rm -f /Library/LaunchDaemons/com.madebyjoe.gpu-wired-limit.plist
  sudo reboot        # the sysctl does not revert until the kernel restarts
  ```
  Simply unsetting the variable does **not** remove the daemon — the task is a
  no-op when undefined, which means it does nothing at all, including cleanup.
  That is deliberate (the brief specifies a complete no-op) but it does mean
  removal is the manual step above.
- **`scripts/soak_small_candidate.sh`** makes requests and writes one report
  file. It never pulls, evicts, or changes an alias, so it has nothing to roll
  back.
