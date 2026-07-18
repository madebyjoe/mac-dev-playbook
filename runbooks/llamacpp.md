# Runbook: llama.cpp inference services

**What it is.** llama.cpp is the fleet's **second** engine (F6.1) for HF-only
distributions, custom quants, and long-context `medium` work where MLX's TTFT
penalty is measured to be unacceptable. Unlike Ollama (one service for every
model), each `engine: llamacpp` manifest entry runs as its **own** launchd
`llama-server` instance: label `com.llamacpp.<safe-name>` (colons/slashes in the
model name become `_`), plist at `~/Library/LaunchAgents/com.llamacpp.<safe-name>.plist`,
rendered by `tasks/configure-llamacpp.yml` from `templates/llamacpp-server.plist.j2`.
Each binds **loopback by default** (F2; an inference role sets `lan_ip`, G30 = LAN), serves
its manifest **`port`**, and offloads all layers to the Apple Silicon GPU
(`--n-gpu-layers -1`). The GGUF quant is downloaded by the storage-aware planner's
pull step (`huggingface-cli download <hf_repo> <quant_file>`) into
`~/.cache/llamacpp/models/`; present-check is file existence. Logs are in
`~/Library/Logs/llamacpp/<safe-name>.{log,err}`. The manifest's top-level `ports:`
registry prevents port collisions — the planner warns on duplicate or
registry-mismatched ports. This runs only on inference-role hosts that actually
have llamacpp entries for their role; the current fleet manifest has none.

## Commands

```sh
# Provision (renders plists; pulls + loads only with model_pull_dry_run=false, -K not needed)
ansible-playbook main.yml --limit headless --tags llamacpp,models -e model_pull_dry_run=false

# Status / restart / stop for one instance (bootstrap/bootout, gui/$UID domain)
uid=$(id -u); label=com.llamacpp.<safe-name>
launchctl print "gui/${uid}/${label}"
launchctl kickstart -k "gui/${uid}/${label}"                                  # restart
launchctl bootout   "gui/${uid}/${label}"                                     # stop
launchctl bootstrap "gui/${uid}" ~/Library/LaunchAgents/${label}.plist        # start

# Verify the OpenAI-compatible endpoint and tail logs
curl -s http://127.0.0.1:<port>/v1/models
tail -f ~/Library/Logs/llamacpp/<safe-name>.log
```

## Swapping a quant

Edit the entry's `quant_file` (and `size_gb`) in `model_manifest.yml`, then re-run
`ansible-playbook main.yml --limit <host> --tags llamacpp,models -e model_pull_dry_run=false`.
The new GGUF is downloaded and the plist re-rendered (which reloads the service).
**The old GGUF is left in place** — the system never deletes; remove it by hand
from `~/.cache/llamacpp/models/` once you've confirmed the swap.

## Rollback

See `docs/rollback-notes.md` (T14). `bootout` the instance, remove its plist, and
`git revert` the T14 commits. Downloaded GGUFs are never deleted by any code path.
