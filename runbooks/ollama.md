# Runbook: Ollama inference service

**What it is.** Ollama runs as a per-user launchd service (label `com.ollama.serve`,
plist at `~/Library/LaunchAgents/com.ollama.serve.plist`) rendered by
`tasks/configure-ollama.yml` from `templates/ollama.plist.j2`. It serves the local
inference API that the Unraid LiteLLM router fronts. It is configured only on
non-`work` hosts that have `ollama` installed; **work-profile machines never run
it** (F3). Where it binds is decided by inference-role membership, not by the
profile: with no role group it binds **loopback `127.0.0.1:11434`** (Ollama has no
auth, so a non-loopback bind would expose an unauthenticated API); a host in
`inference_small`/`inference_medium` binds its **LAN IP** (`ollama_bind`, from
`lan_ip` in `.env`; G30 = LAN) and pins the model resident with
`OLLAMA_KEEP_ALIVE=-1` (G19). Tailscale on the Macs is management-only.
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
```

## Using ollama locally on a role node

An inference-role host binds **`<lan_ip>:11434` only** (G30), **not** `127.0.0.1`.
So on that box the `ollama` CLI and the menu-bar **Ollama.app** — which default to
`127.0.0.1:11434` — will look like "ollama isn't running." It is; it's just on the
LAN IP. This is expected, not a failure (the provisioning run prints where it is
serving).

```sh
# point the CLI at the LAN bind (add to ~/.zshrc for every new shell)
export OLLAMA_HOST=<lan_ip>:11434
ollama list

# one-off without exporting:
OLLAMA_HOST=<lan_ip>:11434 ollama list

# health check (works from this box or the Unraid router over the LAN):
curl -s http://<lan_ip>:11434/api/tags
```

The menu-bar **Ollama.app** cannot be pointed at a non-loopback server; quit it on
a dedicated backend (`osascript -e 'quit app "Ollama"'`). If you need loopback and
LAN both, that host should not be a role node (bind stays loopback, F2) — or accept
`0.0.0.0` as an explicit per-host exception.

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
sets `OLLAMA_HOST`, except `com.ollama.serve.plist`) is booted out and **renamed
to `*.plist.disabled-by-playbook`** — not deleted. To restore one, rename it back
and `bootstrap` it (and bootout `com.ollama.serve` first if you want the old one
to own the port). The menu-bar **Ollama.app** is not a LaunchAgent and is not
touched — quit it manually if it is also serving `:11434`.

## Rollback

See `docs/rollback-notes.md` (T7). In short: `launchctl bootout` the new label,
remove `com.ollama.serve.plist`, restore any `*.disabled-by-playbook` you want
back, `git revert` the T7 commit, then re-render and `bootstrap`. The `pmset -c
sleep 0` change on inference nodes is not auto-reverted — restore with
`sudo pmset -c sleep <minutes>`.
