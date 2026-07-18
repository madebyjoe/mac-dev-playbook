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

## Rollback

See `docs/rollback-notes.md` (T7). In short: `launchctl bootout` the new label,
remove `com.ollama.serve.plist`, `git revert` the T7 commit to restore the old
`com.ollama` plist, then re-render and `bootstrap` it. The `pmset -c sleep 0`
change on inference nodes is not auto-reverted — restore with
`sudo pmset -c sleep <minutes>`.
