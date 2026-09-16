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

Plus one plist key that is not a flag but is just as load-bearing:

| Key | Value | Why |
|---|---|---|
| `WorkingDirectory` | `whisper_work_dir` (`~/.cache/whisper/run`) | `--convert` stages a scratch WAV at a relative path; launchd's default `/` is read-only |
| `EnvironmentVariables.PATH` | `/opt/homebrew/bin:/usr/bin:/bin` | launchd's default PATH cannot see Homebrew, so `--convert` could not find ffmpeg |

**`--convert` also needs a writable working directory — this is the non-obvious
one.** whisper-server stages the uploaded audio at a *relative* path
(`./whisper-server-<timestamp>.wav`) and then execs ffmpeg on it. launchd's
default working directory is `/`, which on modern macOS is a **read-only**
filesystem, so every conversion fails:

```
HTTP 500  {"error":"FFmpeg conversion failed."}
transcribe.log:  Error opening input file ./whisper-server-20260915-205331-133261311.wav.
```

ffmpeg being on `PATH` is **necessary but not sufficient** — the process finds it,
runs it, and ffmpeg then cannot write its own output. The plist therefore sets
`WorkingDirectory` to `whisper_work_dir` (`~/.cache/whisper/run`), created by the
role. Symptom to recognise: *both* WAV and non-WAV uploads return 500 identically.
A conversion-only fault would fail the m4a and pass the WAV.

Scratch files are whisper-server's to clean up; if a crash leaves some behind they
accumulate in `~/.cache/whisper/run` and can be deleted freely while the service
is stopped.

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

**The open question from router-package note N4 is now answered.** Checked against
the box on **2026-09-15**, `whisper-cpp 1.9.1`:

> **`whisper-server` has NO `--api-key` flag — and no auth flag of any kind.**
> `--help` lists 54 options; the only matches for key/token/auth are
> `--max-context`, `--print-special`, `--suppress-nst` and friends, all unrelated.

So there is **no option to authenticate this port**, and the posture above is the
only one available rather than a choice. `config.yaml` sending `api_key: "none"`
is honest, and the firewall scoping to the Unraid source is load-bearing, not
belt-and-braces. Treat `:8082` as fully open to anything that can reach the LAN IP.

Every provisioning run re-probes `whisper-server --help` and prints what it found,
so a future build that *gains* the flag will say so instead of silently leaving
this note stale. If that happens, `whisper_api_key` renders `--api-key` into the
plist and the router's `api_key` must change to match. **Re-check after any
`brew upgrade whisper-cpp`.**

## Taking over from a hand-rolled whisper service

A rev-1 plist from the llama-server era (e.g. `co.onetycho.whisper.transcribe.plist`)
still owning `:8082` is exactly the shape of the original fault: while it holds the
port, the managed service cannot bind and the alias keeps 404ing. Every
`~/Library/LaunchAgents/*.plist` that mentions whisper or serves `/inference`,
except `com.inference.transcribe.plist`, is booted out and **renamed to
`*.plist.disabled-by-playbook`** — never deleted. To restore one, rename it back
and `bootstrap` it (booting out `com.inference.transcribe` first if you want the
old one to own the port).

## Restarting it: the launchd race

`bootout` is **asynchronous**. Bootstrapping before it finishes returns

```
Bootstrap failed: 5: Input/output error
```

and leaves `:8082` **down** — the service is gone and nothing replaced it. The
`Restart transcribe` handler therefore drains first (polls `launchctl print`
until the label disappears) and then retries the bootstrap, because unlike ollama
this service holds a listening socket that can linger for a second or two after
the job itself is gone. If you bounce it by hand, do the same:

```sh
uid=$(id -u); label=com.inference.transcribe
launchctl bootout "gui/${uid}/${label}" 2>/dev/null || true
while launchctl print "gui/${uid}/${label}" >/dev/null 2>&1; do sleep 0.3; done
launchctl bootstrap "gui/${uid}" ~/Library/LaunchAgents/${label}.plist
```

`launchctl kickstart -k` does not have this problem and is the better verb for a
plain restart — use it whenever the plist itself has not changed.

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
