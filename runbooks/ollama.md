# Runbook: Ollama inference service

**What it is.** Ollama runs as a per-user launchd service (label `com.ollama.serve`,
plist at `~/Library/LaunchAgents/com.ollama.serve.plist`) rendered by
`tasks/configure-ollama.yml` from `templates/ollama.plist.j2`. On a host with
`ollama_dev_enabled` (the M1 Pro) a **second** instance, `com.ollama.dev` on
:11435, is rendered from the same template — see *Two instances* below.

It serves the local
inference API that the Unraid LiteLLM router fronts. It is configured only on
non-`work` hosts that have `ollama` installed; **work-profile machines never run
it** (F3). Where it binds is decided by inference-role membership **and the host's
transport**, not by the profile: with no role group it binds **loopback
`127.0.0.1:11434`** (Ollama has no auth, so a non-loopback bind would expose an
unauthenticated API); a host in `inference_small`/`inference_medium` binds
`inference_bind_ip`, which is its **LAN IP** under `inference_transport: lan`
(from `lan_ip` in `.env`) and **`127.0.0.1`** under `inference_transport:
tailnet` (a roaming host — see `runbooks/inference-front.md`). G30 r4. It pins
the model resident with
`OLLAMA_KEEP_ALIVE=-1` (G19) and caps concurrent model slots with
`OLLAMA_MAX_LOADED_MODELS`. **Every `OLLAMA_*` variable is set in the plist's
`EnvironmentVariables` dict, never with `launchctl setenv`** — `setenv` does not
survive a reboot and does not reliably reach a launchd-started daemon, which is
exactly how this box ended up with keep-alive set but the slot cap unset.
Tailscale on the Macs is management-only.
Logs are in `~/Library/Logs/ollama/{ollama.log,ollama.err}`. The plist is bounced
**only when its rendered content changes** (via the `Restart Ollama` handler), so
normal runs never drop a resident model.

## Commands

```sh
# Provision / re-render (loopback host); add -K on an inference node for pmset.
ansible-playbook main.yml --limit headless --tags config,ollama

# Status / restart / stop (bootstrap/bootout replace deprecated load/unload)
launchctl print "gui/$(id -u)/com.ollama.serve"
launchctl kickstart -k "gui/$(id -u)/com.ollama.serve"     # restart
launchctl bootout   "gui/$(id -u)/com.ollama.serve"        # stop/unload
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.ollama.serve.plist  # start/load

# Verify the bind and tail logs
curl -s http://127.0.0.1:11434/api/tags        # loopback host
tail -f ~/Library/Logs/ollama/ollama.log

# The other two labels on an inference_small host. Same verbs, own logs:
#   com.ollama.dev   -> ollama-dev.{log,err}    (the dev zoo, :11435)
#   com.ollama.warm  -> ollama-warm.{log,err}   (periodic warm-keeper)
launchctl print "gui/$(id -u)/com.ollama.dev"
launchctl kickstart -k "gui/$(id -u)/com.ollama.warm"   # warm the pinned models now
```

## Two instances: pipeline (:11434) and dev zoo (:11435)

**Why.** Diagnosed on the M1 Pro 2026-09-12. `small` was answering a ~200-token
prompt in 16s / 21s / 30s / 49s / **76s** while the M4 Pro's much larger `medium`
answered the same prompt in 2.5–3.0s every time, and `embed` returned
`500 {"error":{"message":"Compute error."}}` on **2 of 3** attempts *immediately
after* a `small` call (6/6 pass in isolation — the fault was ordering-dependent,
not random). `/api/ps` explained it: neither pipeline model was resident. A rolling
cast of ad-hoc models — `glm-ocr`, `gemma4:e2b-mlx`, `gemma4:e4b-mlx`, `ornith:9b`,
`translategemma:4b` — was occupying the slots, so every pipeline call paid a full
cold model load.

**The thing that is easy to get wrong:** `OLLAMA_KEEP_ALIVE=-1` was already set and
working (`expires_at` reads year 2318, ollama's "never unload" sentinel). It made
no difference, because **`-1` only defeats the idle timer.** At the slot cap ollama
evicts the *least-recently-used* model regardless of keep-alive. Pipeline traffic
is bursty; a human experimenting is continuous; so the LRU victims were always
`small` and `embed`. The `Compute error.` 500 is the same cause at its sharpest
edge — a Metal allocation failure loading `qwen3-embedding:4b` while a freshly
loaded `qwen3.5:9b-mlx` and a dev model were still resident.

**The fix is the split, not the keep-alive.**

| | `com.ollama.serve` | `com.ollama.dev` |
|---|---|---|
| Port | `<lan_ip>:11434` | `<lan_ip>:11435` |
| Holds | alias models only (`tier: pipeline`) | the ad-hoc zoo (`tier: dev`) |
| `OLLAMA_MAX_LOADED_MODELS` | `ollama_max_loaded_models` (2 on the M1 Pro) | `ollama_dev_max_loaded_models` (1) |
| `OLLAMA_KEEP_ALIVE` | `-1` (never unload) | `10m` — **finite on purpose** |
| Model store | `~/.ollama/models` | the same store, shared |

Both instances share `~/.ollama/models`, so a model is downloaded once; only
**residency** is partitioned, which is the thing that was actually contended. All
pulls go through the pipeline instance so writes to the shared blob store stay
single-writer.

Note `-1` on the dev instance would rebuild the original fault inside the dev
zoo — an experiment must release its slot on its own.

**Slot budget arithmetic (M1 Pro, 32 GB).** Pipeline floor is
`qwen3.5:9b-mlx` 8.12 GB + `qwen3-embedding:4b` ~2.5 GB ≈ **10.6 GB**, so two
slots. One dev slot adds at most `gemma4:e4b-mlx` 8.47 GB → ~19 GB of 32 GB,
leaving headroom for KV cache and the OS. **Do not raise either cap without the
unified memory to match** — an over-subscribed cap makes the `Compute error.` 500
*more* frequent, not less. The M4 Pro runs one slot and no dev instance.

**Router side.** Every `dev/*` entry in the router's `config.yaml` must point at
`:11435`. The emitted snippet (`artifacts/litellm/<host>.yml`) already does this:
dev-tier entries render under the `dev/` namespace on the dev port, pipeline and
standby entries on `:11434`. Pointing one dev entry back at `:11434` re-creates
the whole fault.

## Keeping the pipeline models resident

Two mechanisms, and the difference matters:

- **`com.ollama.warm`** (`StartInterval`, default 240s) runs
  `~/.cache/mac-dev-playbook/ollama-warm.sh`, which issues a zero-work load
  request for each pinned model (`/api/generate` with an empty prompt, or
  `/api/embed` with empty input for embedding models). This is a **mitigation**:
  it just guarantees a pipeline model is never the LRU candidate. It cannot help
  an over-subscribed slot budget.
- **The assertion** (`tasks/verify-ollama-residency.yml`, run *after* the model
  pulls — asserting before them would fail for the wrong reason). It warms
  synchronously, reads `/api/ps`, and **fails the run** unless every
  `tier: pipeline` model is resident. The invariant is checked, not trusted. It
  is skipped under `--check` and when `model_pull_dry_run` is true (the models may
  not be on disk yet); pass `-e skip_residency_check=true` to bypass it
  deliberately. Run it alone with `--tags verify`.

```sh
# The invariant, by hand. Must list BOTH pipeline models.
curl -s http://<lan_ip>:11434/api/ps | jq -r '.models[].name'

# What the dev instance is holding (should never be a pipeline model)
curl -s http://<lan_ip>:11435/api/ps | jq -r '.models[].name'

# Warm-keeper status and its last run
launchctl print "gui/$(id -u)/com.ollama.warm"
tail -20 ~/Library/Logs/ollama/ollama-warm.log
```

**The real acceptance test is time-based:** `ollama ps` after a ~6-hour idle
period still lists both pipeline models. A fix that only holds while you are
watching it is not a fix. Run the router-side `smoke-test.sh` **at least three
times** as well — check 5 (`embed`) is deliberately ordered *after* the chat
checks because `embed` in isolation passes 6/6 and would hide the fault.

## Using ollama locally on a role node

**This depends on the host's transport (G30 r4), and the two cases are opposite.**

### On a `tailnet` (roaming) host — e.g. the M4 Pro

Ollama binds **`127.0.0.1:11434`**, so the `ollama` CLI, the menu-bar
**Ollama.app**, Raycast and IDE integrations all work with **no configuration and
no network at all**. That is the point: on a plane, `medium` still answers
locally.

**F-MDP4-6 — local tools address `http://127.0.0.1:11434` DIRECTLY.** Not through
the Caddy front on `:11436`, and not through LiteLLM on the router. This is a
deliberate, written exception to the standing "reference aliases, never hardware"
rule, and the reasoning is narrow:

- an IDE or a launcher is **not a pipeline** — nothing downstream depends on it
  having gone through the router's alias indirection, key enforcement or logging;
- **offline operation is the entire reason this host moved to loopback.** Routing
  a local tool via the tailnet front would make it depend on `tailscaled` being
  up and would break in exactly the situation the design exists to survive.

Pipelines still go through the router and its aliases. Only on-box interactive
tools take this exception. (Repointing them is human track H6.)

```sh
# Nothing to export. This just works, online or off.
ollama list
curl -s http://127.0.0.1:11434/api/tags
```

**MIGRATING A HOST FROM `lan` TO `tailnet`: delete the old `OLLAMA_HOST` export.**
This bites, and it bites *silently*. The `lan` instructions further down tell you
to put `export OLLAMA_HOST=<lan_ip>:11434` in `~/.zshrc`, which was correct then.
After the transport flips it is actively wrong: the service is on loopback, but
every new shell still points the CLI at a LAN address the laptop may not hold, so
`ollama list` fails with an i/o timeout while the service is perfectly healthy.
Nothing in the playbook can catch this — **this repo does not manage your
dotfiles**, so the rendered plist and the shell disagree and only the shell is
wrong.

```sh
# Find it (checks the interactive and login files, and the system-wide ones).
for f in /etc/zshenv /etc/zprofile /etc/zshrc ~/.zshenv ~/.zprofile ~/.zshrc; do
  [ -f "$f" ] && grep -Hn '^[^#]*OLLAMA_HOST' "$f"
done

# GUI apps (Raycast, IDEs) read this instead of your shell rc -- check it too.
launchctl getenv OLLAMA_HOST

# Remove the line, then drop it from shells that are already open:
unset OLLAMA_HOST
launchctl unsetenv OLLAMA_HOST     # only if the above printed something
```

Verify with a CLEAN shell, not the one you are sitting in — a shell that already
exported the variable keeps it, and a subshell inherits it, so
`zsh -l -c 'echo $OLLAMA_HOST'` will happily show you the stale value and look
like the fix failed:

```sh
env -u OLLAMA_HOST zsh -l -c 'echo "${OLLAMA_HOST:-unset (correct)}"; ollama list'
```

The remote path is separate and is documented in `runbooks/inference-front.md`.

### On a `lan` (stationary) host — e.g. the M1 Pro

An inference-role host on the `lan` transport binds **`<lan_ip>:11434` only**,
**not** `127.0.0.1`. So on that box the `ollama` CLI and the menu-bar
**Ollama.app** — which default to `127.0.0.1:11434` — will look like "ollama isn't
running." It is; it's just on the LAN IP. This is expected, not a failure (the
provisioning run prints where it is serving).

This also applies to the playbook itself: `ollama list` and `ollama pull` are API
clients, so `tasks/pull-models.yml` exports `OLLAMA_HOST=<lan_ip>:11434` for the
planner and every pull. Without it they fail with "could not connect to ollama
app" on a LAN-bound node.

```sh
# point the CLI at the LAN bind (add to ~/.zshrc for every new shell)
export OLLAMA_HOST=<lan_ip>:11434
ollama list

# the dev zoo is a separate server — address it explicitly
OLLAMA_HOST=<lan_ip>:11435 ollama ps

# one-off without exporting:
OLLAMA_HOST=<lan_ip>:11434 ollama list

# health check (works from this box or the Unraid router over the LAN):
curl -s http://<lan_ip>:11434/api/tags
```

The menu-bar **Ollama.app** cannot be pointed at a non-loopback server; quit it on
a dedicated backend (`osascript -e 'quit app "Ollama"'`).

If you need loopback **and** remote reach on the same host, that is what the
`tailnet` transport is for — see above. **`0.0.0.0` is no longer available as a
per-host exception**: `tasks/load-local-env.yml` fails the run on any wildcard
bind, on every host and every transport, with no override flag (F-MDP4-3). An
unauthenticated inference API on every interface is not something this repo will
render, and on a machine that joins untrusted networks it is the specific thing
the design exists to prevent.

## Apple Silicon backend: MLX vs GGML (Amendment 1 A4.5)

As of Ollama 0.19 the Apple Silicon backend is **MLX**, which is why the manifest
`small`-role tags are the `-mlx` artifacts (20–87% faster than GGML-Metal below
14B). MLX documents a **32 GB unified-memory floor**, and the M1 Pro sits exactly
at it: if MLX fails to activate there, the node **silently falls back to GGML**.
So after any Ollama upgrade, **verify which backend is actually running on the
M1 Pro and record it here** — trust the observed backend over any article
claiming the preview is M5-only.

```sh
# The backend is reported in the ollama server logs at startup / first load.
grep -iE 'mlx|ggml|metal' ~/Library/Logs/ollama/ollama.log | tail -20
```

**Observed backend (M1 Pro, update after each upgrade):** _not yet recorded._

> MLX does a full prefill before the first token, so TTFT grows linearly with
> input length. That penalty is the measurement trigger for routing long-context
> `medium` jobs to llama.cpp instead (F6.1, pending T14) — see
> `runbooks/model-management.md`.

## Taking over from a hand-rolled ollama service

If the machine already ran ollama from a manually-created launchd service (e.g. a
`com.ollama` or `local.ollama` plist binding `0.0.0.0`, or `brew services start
ollama`), the playbook **disables it** so `com.ollama.serve` can own `:11434` —
otherwise the old service keeps the port and the LAN bind never takes effect.
Each conflicting `~/Library/LaunchAgents/*.plist` (any that runs `ollama serve` /
sets `OLLAMA_HOST`, except the three this playbook owns — `com.ollama.serve`,
`com.ollama.dev`, `com.ollama.warm`) is booted out and **renamed
to `*.plist.disabled-by-playbook`** — not deleted. To restore one, rename it back
and `bootstrap` it (and bootout `com.ollama.serve` first if you want the old one
to own the port). The menu-bar **Ollama.app** is not a LaunchAgent and is not
touched — quit it manually if it is also serving `:11434`.

## No cloud tags, no `ollama signin`

**`ollama signin` must never be run on an inference host, and no `-cloud` tag may
ever be pulled.** Those tags transparently proxy inference off-LAN to Ollama's
hosted service — one typo'd tag defeats the entire architecture silently. Two
guards enforce this: `plan_model_pulls.py --lint` (run in CI and before the router
snippet is rendered) fails on any `-cloud` name, and `tasks/pull-models.yml`
re-asserts it on the target immediately before pulling. Nothing in this playbook
ever invokes `ollama signin`, and nothing here needs an account.

## Rollback

See `docs/rollback-notes.md` (T7 for the original service, "M1 Pro remediation"
for the two-instance layout). In short: `launchctl bootout` the labels, remove the
plists, restore any `*.disabled-by-playbook` you want back, `git revert`, then
re-render and `bootstrap`.

```sh
uid=$(id -u)
for l in com.ollama.warm com.ollama.dev; do
  launchctl bootout "gui/${uid}/$l" 2>/dev/null || true
  rm -f ~/Library/LaunchAgents/$l.plist
done
```

Setting `ollama_dev_enabled: false` does the same thing on the next run (the dev
instance is booted out and its plist removed). Reverting to a single instance
means the dev zoo shares `:11434` again — re-read the top of this runbook before
deciding that is what you want. The `pmset -c sleep 0` change on inference nodes
is not auto-reverted — restore with `sudo pmset -c sleep <minutes>`.
