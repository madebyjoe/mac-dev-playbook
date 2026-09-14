# Runbook: verification (two-layer test model)

**The model.** Inference is verified at **two layers, one owner each**, so a
failure localizes cleanly instead of "something's broken somewhere":

| Layer | Owner | What it proves |
|---|---|---|
| **Backend liveness** | `scripts/probe_backends.py` (this repo) | Each Mac backend is reachable on its port(s): ollama `:11434`, the dev-zoo ollama `:11435` where one exists, transcribe `:8082`, any llamacpp ports. Answers **"backend down vs. router broken."** |
| **Alias end-to-end** | router-side `smoke-test.sh` (Brief 1 package, in personal Gitea; **not in this repo**) | Each LiteLLM **alias** returns completions with a virtual key, and the router degrades correctly. |

Neither duplicates the other; run the backend layer after provisioning a Mac, and
the alias layer after router config changes and for drills. **Latency is reported,
never asserted** — G19 budgets belong to Brief 12.

## Backend layer — `probe_backends.py`

```sh
python3 scripts/probe_backends.py                 # probe every inference node in .env + inventory
python3 scripts/probe_backends.py --only mac-headless
python3 scripts/probe_backends.py --expect-down mac-headless   # drill D10 backend half
```

It reads LAN IPs from `.env`, role membership from `inventory`, and the port set
from `model_manifest.yml` (11434 per ollama host + **11435 for the second "dev
zoo" ollama instance** on any role that has `tier: dev` entries + 8082 for the M1
Pro whisper service + any llamacpp ports; F13 r3). A probe **PASSES** when the
server answers with any HTTP status; it **FAILS** on connection refused/timeout.
Exit is non-zero on any unexpected state, including under `--expect-down` (up when
it should be down → FAIL).

For a **functional** transcribe check (beyond liveness), POST synthetic audio to
the canonical endpoint (`--inference-path`, T17 r4) — a real transcript back
proves the OpenAI path + server-side conversion, not just reachability:

```sh
curl -s http://<m1pro_lan_ip>:8082/v1/audio/transcriptions -F file=@test.wav -F model=whisper-large-v3-turbo
```

Liveness is **not** enough for the two faults this repo most recently fixed, so
both have their own checks — run at provisioning time, and repeatable by hand:

```sh
# transcribe served the wrong PATH while answering 200 on /. A 404 here means
# --inference-path was dropped; any other status means the route exists.
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  -F file=@test.wav -F model=whisper \
  http://<m1pro_lan_ip>:8082/v1/audio/transcriptions

# the residency invariant: BOTH pipeline models must be listed
curl -s http://<m1pro_lan_ip>:11434/api/ps | jq -r '.models[].name'
```

For the residency invariant the **acceptance test is time-based**: `ollama ps`
after a ~6-hour idle period still lists both. And run the alias layer **at least
three times** — the `embed` failure it guards against was intermittent at 2-in-3,
so one green run proves nothing. Check 5 runs *after* the chat checks on purpose:
`embed` in isolation passes 6/6 and would hide the fault.

## Alias layer — router-side `smoke-test.sh` (cross-reference)

> **Reconcile first (parallel-track P5).** The router package's `config.yaml` was
> authored before the F6.1 engine decision and points `small`/`medium` at a
> `llama-server :8081`. Before running the alias layer, diff the playbook-emitted
> snippet (`artifacts/litellm/<host>.yml`) against the live `config.yaml` and
> correct **every** `api_base` to the ports this repo serves — ollama `:11434`
> (`small`, `medium`, `embed`) and whisper `:8082` (`transcribe`) — then
> `docker compose restart litellm`. The snippet is the verification artifact; the
> human edits the live config.

Run it on Unraid with the low-privilege `vk-smoke` virtual key (G32), never the
master key. It owns every Brief 1 acceptance criterion so nothing is orphaned
between the two layers:

- **completions per alias** (`small`, `medium`, `transcribe`, `embed`) → alias layer.
- **clean 503 when a backend is down** → alias layer; `probe_backends.py --expect-down <host>` confirms the **backend half** of the same drill (**D10 — backend unplug**).
- **400 on the unconfigured `large` alias** → alias layer.
- **401 unauthenticated / 401-403 for `vk-smoke` on a non-allow-listed alias** (negative-auth) → alias layer (G32 rider).
- **embed dimension tripwire** (print the returned vector length; drift is the early warning for schema invalidation) → alias layer.

## Drill D10 — backend unplug

1. Pull one Mac's network cable (or stop its services).
2. Backend half: `python3 scripts/probe_backends.py --expect-down <that-host>` → must exit 0 (its probes fail, the rest pass).
3. Alias half: router-side `smoke-test.sh` → that Mac's aliases return a clean 503, the others still complete.

## Standing reminder — firewall validation

Per-interface firewall rules must be validated with **Tailscale disabled on the
test client**. Under **G30 = LAN** both test layers keep working during those
windows (backend traffic is wired-LAN, not tailnet-carried), so you can validate
Task 9 rules without taking inference down.

## Rollback

Additive — `git revert` the T18 commit; nothing depends on it.
