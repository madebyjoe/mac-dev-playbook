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
`inference_small` host (work guard applies). It serves the OpenAI-shaped path
`/v1/audio/transcriptions` (via `--inference-path`) so the router's `openai/`
mapping reaches it without a rewrite, and converts non-WAV input server-side (via
`--convert` + ffmpeg on the launchd `PATH`), making the alias format-agnostic for
every caller — Brief 9's iOS Voice Memos m4a included (T17 r4, F16/F17).

## Why the whole argument vector is templated

`transcribe` was hard down for a while, 404ing on every `smoke-test.sh` check 6:

```
litellm.NotFoundError: NotFoundError: OpenAIException -
  File Not Found (/v1/audio/transcriptions). Received Model Group=transcribe
```

That 404 was **whisper-server's own**, not the router's — LiteLLM was faithfully
reporting what the backend returned. `curl http://<lan_ip>:8082/` answered 200, so
the process, the network path and the firewall were all fine. The running server
had simply been started by hand **without `--inference-path`**, so whisper.cpp
served its default `/inference` while LiteLLM called `/v1/audio/transcriptions`.

Hence: the argument vector is rendered from variables into a managed plist, so no
flag can be silently dropped by a hand-edit; the run **takes over** any hand-rolled
whisper LaunchAgent (see below); and the run **verifies the path** before it
finishes. A 404 to an empty POST at `/v1/audio/transcriptions` now fails
provisioning instead of surfacing days later as a router smoke-test failure.

## Dependencies and flags

| Flag | Value | Why |
|---|---|---|
| `--model` | `~/.cache/whisper/models/ggml-large-v3-turbo.bin` | from the manifest entry |
| `--host` | `lan_ip` | never `0.0.0.0` — this port has no auth |
| `--port` | `8082` | manifest `ports:` registry |
| `--inference-path` | `/v1/audio/transcriptions` | the OpenAI path the router calls |
| `--convert` | — | server-side format conversion, needs **ffmpeg** |
| `--threads` | `whisper_threads`, default `8` | the M1 Pro's performance cores |

**ffmpeg is a hard dependency of this role, not an assumption.** `--convert`
shells out to it for anything that is not already 16 kHz mono WAV, and Home
Assistant / Wyoming will not always send a clean one. It is installed by this role
alongside `whisper-cpp` (it is also in `core_homebrew_packages`, but the role must
not depend on another list staying that way), and the run asserts
`/opt/homebrew/bin/ffmpeg` exists — the same directory the plist pins on the
launchd `PATH`, since launchd's default `PATH` cannot see Homebrew. A missing
ffmpeg now fails provisioning rather than every non-WAV upload at request time.

## Auth posture: port 8082 is unauthenticated

Stated out loud rather than left implicit, because `config.yaml` sends
`api_key: "none"` to this backend and that is only honest if someone checked.

**As deployed, port 8082 is unauthenticated: the service is started with no
`--api-key`, so the LAN bind plus the firewall scope to the Unraid source is its
only protection.** The LiteLLM router at `:4000` is the enforcement door for
clients; the backend port is not exposed to them. That much is true regardless of
what the binary supports, and it is what makes `api_key: "none"` honest.

**Whether the installed build even has an `--api-key` flag has not been confirmed
against the box** (router-package note N4 flagged it; nobody has checked). So the
run checks instead of guessing: every provisioning run probes
`whisper-server --help` on the target and prints which case it found —

- *no flag* → the posture above is the only one available; nothing to decide.
- *flag present* → port 8082 is unauthenticated **by choice**. Set
  `whisper_api_key` to render `--api-key` into the plist, and change the router's
  `api_key` to match.

**Record the answer here after the next run**, and re-check after any
`brew upgrade whisper-cpp`:

> **Probed `--api-key` support (update after each run):** _not yet recorded._

## Taking over from a hand-rolled whisper service

A rev-1 plist from the llama-server era (e.g. `co.onetycho.whisper.transcribe.plist`)
still owning `:8082` is exactly the shape of the original fault: while it holds the
port, the managed service cannot bind and the alias keeps 404ing. Every
`~/Library/LaunchAgents/*.plist` that mentions whisper or serves `/inference`,
except `com.inference.transcribe.plist`, is booted out and **renamed to
`*.plist.disabled-by-playbook`** — never deleted. To restore one, rename it back
and `bootstrap` it (booting out `com.inference.transcribe` first if you want the
old one to own the port).

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

# Verify the canonical endpoint (LAN IP from .env; :8082) — expects {"text": ...}
curl -s http://<lan_ip>:8082/v1/audio/transcriptions -F file=@test.wav -F model=whisper-large-v3-turbo
tail -f ~/Library/Logs/transcribe/transcribe.log

# Is the OpenAI path actually being served? 404 here == --inference-path was
# dropped and the alias is hard down. Any other status means the route exists.
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  -F file=@test.wav -F model=whisper \
  http://localhost:8082/v1/audio/transcriptions     # expect 200

# Does this build have auth at all?
whisper-server --help | grep -- --api-key || echo "no --api-key on this build"
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
