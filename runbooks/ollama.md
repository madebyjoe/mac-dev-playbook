# Runbook: Ollama inference service

**What it is.** Ollama runs as a per-user launchd service (label `com.ollama.serve`,
plist at `~/Library/LaunchAgents/com.ollama.serve.plist`) rendered by
`tasks/configure-ollama.yml` from `templates/ollama.plist.j2`. It serves the local
inference API that the Unraid LiteLLM router fronts. It is configured only on
non-`work` hosts that have `ollama` installed; **work-profile machines never run
it** (F3). Where it binds is decided by inference-role membership, not by the
profile: with no role group it binds **loopback `127.0.0.1:11434`** (Ollama has no
auth, so a non-loopback bind would expose an unauthenticated API); a host in
`inference_small`/`inference_medium` binds its **Tailscale IP** (`ollama_bind`,
from `tailnet_ip`) and pins the model resident with `OLLAMA_KEEP_ALIVE=-1` (G19).
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

## Rollback

See `docs/rollback-notes.md` (T7). In short: `launchctl bootout` the new label,
remove `com.ollama.serve.plist`, `git revert` the T7 commit to restore the old
`com.ollama` plist, then re-render and `bootstrap` it. The `pmset -c sleep 0`
change on inference nodes is not auto-reverted — restore with
`sudo pmset -c sleep <minutes>`.
