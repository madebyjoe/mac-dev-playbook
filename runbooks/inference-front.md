# Runbook: the roaming inference front (Caddy + `tailscale serve`)

**What it is.** A two-process door that lets the Unraid LiteLLM router reach an
inference host that **travels**, without that host ever binding a routable
address. It exists only on hosts with `inference_transport: tailnet`
(`host_vars/<host>.yml`); a stationary `lan` host has none of this and keeps
reaching its LAN bind directly. Rendered by `tasks/configure-inference-front.yml`
from `templates/Caddyfile.inference-front.j2` and
`templates/inference-front.plist.j2`; verified by `tasks/verify-inference-front.yml`.

```
LiteLLM (Unraid, tailnet node)
   │  http://<tailnet_ip>:11434/v1          ← tailnet only, ACL-restricted
   ▼
tailscale serve  (TCP forward, tailnet :11434 → 127.0.0.1:11436)
   ▼
Caddy  127.0.0.1:11436   (rewrites Host → localhost:11434)
   ▼
Ollama 127.0.0.1:11434   (loopback ONLY, always)
   ▲
Local tools on the laptop (Raycast, IDE) hit 127.0.0.1:11434 directly — works offline.
```

## Why this is not just `tailscale serve` (the 403)

`tailscale serve` in TCP mode forwards the caller's `Host` header **untouched**,
and has no option to rewrite it. Loopback-bound Ollama **validates** `Host`: a
request arriving as `Host: macbook-pro-m4.tailXXXX.ts.net` is refused with
**403** even though it reached the right port. So the router would connect
successfully and get a 403 on every call.

Caddy is a one-purpose shim for exactly that: `header_up Host localhost:11434`.
It does nothing else — no TLS (the tailnet is the encrypted layer), no auth
(the tailnet ACL is the access control), no admin API, no other route.

Two details that look like style and are not:

- The Caddyfile site address **must** be `http://:11436`, never
  `http://127.0.0.1:11436`. Caddy matches sites on the **Host header**, and the
  incoming Host here is the tailnet address — a `127.0.0.1` site address matches
  nothing and every request 404s. `bind 127.0.0.1` inside the block is what
  actually restricts the listener.
- `flush_interval -1` is what keeps streaming streaming. Without it Caddy
  buffers and releases the whole completion at the end; nothing errors, nothing
  logs, and every streaming consumer just looks hung.

## `admin off` ⇒ `caddy reload` does not work here

Caddy's admin API (default `:2019`) is unauthenticated and can rewrite the
running config. On a laptop that joins untrusted networks that is precisely the
door this design exists to close, so the Caddyfile sets `admin off`.

**Consequence:** `caddy reload` needs the admin API and will fail. A Caddyfile
change is applied by **bouncing the launchd job**, which is what the
`Restart inference front` handler does (`launchctl kickstart -k`). The playbook
runs `caddy validate` on the rendered file *before* any restart, because with
`KeepAlive` a bad config becomes a crash-loop that reads as "the front is down"
rather than "the config is wrong".

## Building the front and opening the door are separate steps

The tailnet ACL is the **only** authentication this port has. Opening the door
before that ACL exists publishes an unauthenticated inference API to every node
on the tailnet. So the serve rule is tagged `tailnet-door` and can be held back:

```sh
# Build and verify the loopback front. Nothing becomes network-reachable:
# ollama stays on 127.0.0.1:11434, Caddy on 127.0.0.1:11436, checks a-d run.
ansible-playbook main.yml --limit personal --tags inference-front --skip-tags tailnet-door

# Open the door, once the router is a tailnet node (H1) and the ACL restricting
# tag:llm-router -> tag:inference-roaming:11434 is in place (H2). Idempotent, so
# this is just a re-run; check (e) runs too.
ansible-playbook main.yml --limit personal --tags inference-front
```

`tailscale serve status` reporting `No serve config` means the door is shut.

## Commands

```sh
# Provision / re-render (roaming host only). Add --skip-tags tailnet-door to
# build the front without publishing anything to the tailnet.
ansible-playbook main.yml --limit personal --tags inference-front

# Status / restart / stop. NOTE kickstart, not `caddy reload` (admin off).
launchctl print     "gui/$(id -u)/com.madebyjoe.inference-front"
launchctl kickstart -k "gui/$(id -u)/com.madebyjoe.inference-front"   # apply a config change
launchctl bootout   "gui/$(id -u)/com.madebyjoe.inference-front"      # stop
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.madebyjoe.inference-front.plist

# Config + logs
cat  ~/.config/inference-front/Caddyfile
caddy validate --config ~/.config/inference-front/Caddyfile --adapter caddyfile
tail -f ~/Library/Logs/inference-front/inference-front.err          # crash-loop reasons
tail -f ~/Library/Logs/inference-front/inference-front.access.log   # requests (see N2)

# The tailnet door
tailscale serve status
tailscale serve status --json | python3 -m json.tool
```

## Verifying it by hand (what T-MDP4-6 automates)

```sh
# a. Nothing of ours on a routable address. Every line must be 127.0.0.1.
lsof -nP -iTCP -sTCP:LISTEN | awk '$1=="ollama" || $1=="caddy" {print $1, $NF}'

# b. The Host guard is present — this is WHY Caddy exists. Expect 403.
curl -s -o /dev/null -w '%{http_code}\n' \
  -H 'Host: probe.example.ts.net' http://127.0.0.1:11434/api/version

# c. ...and the front defeats it. Same request, front port. Expect 200.
curl -s -o /dev/null -w '%{http_code}\n' \
  -H 'Host: probe.example.ts.net' http://127.0.0.1:11436/api/version

# d. Streaming is not buffered. Expect MANY lines, not one.
curl -s -N -H 'Content-Type: application/json' \
  -d '{"model":"<a model on this host>","stream":true,"max_tokens":24,
       "messages":[{"role":"user","content":"Count to ten."}]}' \
  http://127.0.0.1:11436/v1/chat/completions | grep -c '^data: '

# e. TCP forward present, Funnel ABSENT.
tailscale serve status
```

If (b) returns **200** instead of 403, Ollama's Host-validation behaviour has
changed and the Caddy hop may be removable — **report it, do not act on it**.
That is a human decision (brief STOP 2); the verify task reports it as a note
and does not fail the run.

## `tailscale funnel` — never

`serve` publishes to **your tailnet**. `funnel` publishes to **the public
internet**. This port is an unauthenticated inference API, so Funnel on this node
would expose it to anyone. `tasks/verify-inference-front.yml` asserts Funnel is
absent from the serve config (F-MDP4-5). If it ever appears:

```sh
tailscale funnel 11434 off
```

## What actually protects this port

**The tailnet ACL, and nothing else.** Ollama has no authentication; the LAN
firewall is not in this path at all any more. The ACL admits only the router's
tag to this node's `:11434`:

```
tag:llm-router → tag:inference-roaming:11434        # and nothing else to that port
```

That ACL file **is** the policy record for this path (human track H2). Two
consequences worth knowing:

- **Attribution is not in the Caddy log (N2).** In TCP mode every request
  reaches Caddy from `127.0.0.1`, so the access log cannot tell you who called.
  Attribution lives in the ACL (only one tag can connect at all) and in LiteLLM's
  own logs.
- **Tailnet traffic is invisible to the Task 9 segmentation matrix.** Accepted
  and documented as part of G30 r4 — the ACL is the compensating record.

## Local tools bypass all of this (F-MDP4-6)

Raycast, IDE integrations and the `ollama` CLI on this laptop use
`http://127.0.0.1:11434` **directly** — not the front, not LiteLLM. This is a
written exception to "reference aliases, never hardware": those are not
pipelines, and the point is that they keep working on a plane with no network.
See `runbooks/ollama.md`.

## Known gap (N3)

`tailscale serve` fronts **the ollama pipeline port only**. Anything else on a
roaming host — the dev zoo (`:11435`), whisper (`:8082`), a future `llama-server`
— binds loopback and has **no remote route**. The emitted router snippet renders
those entries commented out with the reason rather than as live entries the
router would dial and hang on. Giving one a route means a second Caddy site plus
a second serve rule; flag it when F6.1.2 lands on the M4 Pro.

## Rollback

See `docs/rollback-notes.md` (MDP-4). In short:

```sh
tailscale serve --tcp=11434 off
launchctl bootout "gui/$(id -u)/com.madebyjoe.inference-front"
rm -f ~/Library/LaunchAgents/com.madebyjoe.inference-front.plist
rm -rf ~/.config/inference-front
rm host_vars/mac-personal.yml      # host returns to the `lan` default
ansible-playbook main.yml --limit personal --tags config,ollama
```

Then restore the router's `api_base` to the LAN address. Deleting the host_vars
file alone is enough to revert the transport — `inference_transport` defaults to
`lan` in `group_vars/all.yml`.
