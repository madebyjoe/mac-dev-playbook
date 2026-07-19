# Runbook: transcribe (whisper-server) service

**What it is.** The `transcribe` alias is served by **whisper.cpp `whisper-server`**
(brew `whisper-cpp`) on the **M1 Pro**, built from the Brief 1 design — it was
never previously deployed, so this is a build, not an adoption (G31a / F13 r3). It
runs as a launchd service (label `com.inference.transcribe`, plist at
`~/Library/LaunchAgents/com.inference.transcribe.plist`), rendered by
`tasks/configure-transcribe.yml` from `templates/transcribe.plist.j2`. It **binds
the host's LAN IP** on port **8082** (`lan_ip` from `.env`; G30 = LAN) — never
`0.0.0.0`. whisper-server has **no API-key auth** (router-package note N4), so the
only control is firewall-scoping to the Unraid source; the LiteLLM router at
`:4000` is the enforcement door for clients. The GGML model
(`ggml-large-v3-turbo.bin`, from the manifest's `whisper-large-v3-turbo` entry) is
fetched from Hugging Face into `~/.cache/whisper/models/` by the same task,
honoring `model_pull_dry_run` (no separate flag); the planner itself skips
`engine: whisper` for pulls. The service is only (re)started once the model file
exists, so a dry/check run just renders the plist. Logs are in
`~/Library/Logs/transcribe/transcribe.{log,err}`. It is configured only on an
`inference_small` host (work guard applies).

## Commands

```sh
# Provision (renders plist; downloads model + starts only with model_pull_dry_run=false)
ansible-playbook main.yml --limit headless --tags transcribe -e model_pull_dry_run=false

# Status / restart / stop (bootstrap/bootout, gui/$UID domain)
uid=$(id -u); label=com.inference.transcribe
launchctl print "gui/${uid}/${label}"
launchctl kickstart -k "gui/${uid}/${label}"                               # restart
launchctl bootout   "gui/${uid}/${label}"                                  # stop
launchctl bootstrap "gui/${uid}" ~/Library/LaunchAgents/${label}.plist     # start

# Verify and tail logs (LAN IP from .env; :8082)
curl -s http://<lan_ip>:8082/models
tail -f ~/Library/Logs/transcribe/transcribe.log
```

Model file convention: `~/.cache/whisper/models/ggml-<name-without-whisper->.bin`,
where `<name>` is the manifest entry (`whisper-large-v3-turbo` → `ggml-large-v3-turbo.bin`).

> **`embed`** is *not* a separate service: it is served by the existing ollama
> instance on `:11434` (F13 r3 — there is no separate embed server). See
> `runbooks/ollama.md` and `runbooks/model-management.md`.

## Rollback

`bootout` the service and remove the plist:

```sh
launchctl bootout "gui/$(id -u)/com.inference.transcribe" 2>/dev/null || true
rm -f ~/Library/LaunchAgents/com.inference.transcribe.plist
```

Then `git revert` the T17 commit. The downloaded GGML model in
`~/.cache/whisper/models/` is never deleted by any code path — remove it by hand
if you want the space back.
