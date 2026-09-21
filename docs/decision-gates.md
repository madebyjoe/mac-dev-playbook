# Decision gates & fixed decisions (in-repo register)

Tracks the status of the brief's decision gates and the Amendment 1 engine
policy, as they affect this repo. Source of truth for wording is the handoff
brief + `Amendment_1_Engine_Policy_and_G29.md`; this file records what is
ratified/open so it lives with the code.

## Engine policy

- **F6.1 (supersedes F6) — FIXED.** Ollama is the primary engine on all Mac
  inference nodes (MLX backend; fastest for the whole `small` role). llama.cpp
  (`engine: llamacpp`) is the ratified **second** engine for HF-only
  distributions, custom quants, and measured long-context `medium` work. It is
  **implemented as of T14** (planner plans both engines; per-model `llama-server`
  launchd services). "Switch to llama.cpp for MLX" is a category error and is not
  relitigated.

## Gates

| Gate | Status | Notes |
|---|---|---|
| **G25** — model manifest contents | **Ratified** | Human-provided manifest adopted in `model_manifest.yml` (P5-2). Still carries "estimate — verify" flags reconciled by the T10 post-pull verify step. |
| **G26** — disk reserve floor | Default **100 GB** | `reserve_floor_gb` in inference-role group_vars; overridable per host. |
| **G27** — repo origin (Gitea vs GitHub) | **Open** | Human decision; TODO candidate in README. Executor does nothing here. |
| **G28** — core container-runtime pruning | **Open** | `docker` + `colima` + `orbstack` all in core; flagged, not changed beyond F3's moves. |
| **G29** — gaming PC as inference node | **DEFER (default)** | See below. Out of scope for mac-dev-playbook. |
| **G30** — router→backend transport | **RESOLVED (r4) — per-host: `lan` for stationary, `tailnet` for roaming** | Superseded r2/r3 **for roaming hosts only**; stationary hosts are unchanged. See the MDP-4 section below. |
| **G31** — service definitions for :8082 / :8084 | **RESOLVED (r3)** | Neither service was ever deployed. **G31a transcribe** = build whisper-server (`whisper-cpp`), bind `lan_ip:8082`, no auth (T17). **G31b embed** = consolidate onto ollama `:11434`; the standalone `:8084` server is retired/unbuilt — **port 8084 removed everywhere**. |
| **G32** — smoke-test virtual key | **Router-side, human** | `vk-smoke`: low-privilege LiteLLM virtual key allow-listing exactly `small`, `medium`, `transcribe`, `embed` — never `caption-unfiltered`. Created on the router; value goes only into `.env`. The negative-auth assertion lives in the router-side `smoke-test.sh`, not this repo. |
| **G33** — snippet `api_base` naming | **RESOLVED = raw `lan_ip` (v1)** | Backed by static DHCP reservations. Unbound names (`m1pro.…internal`) survive readdressing but add a DNS dependency to the inference path — flagged as a later amendment, not implemented here. |

### G30 firewall consequence — Task 9a rows (r4)

The executor never touches firewall config. Recorded so the Task 8/9 work stays in
sync: the **M1 Pro has moved to the ServerPhysical VLAN**, so all Unraid → M1 Pro
flows (`11434`, `8082`) are now **intra-VLAN — no rows required**.

~~The sole surviving cross-VLAN row is `Unraid (LiteLLM) → M4PRO_HOST : 11434/tcp`.~~
**STRUCK under G30 r4.** The M4 Pro is a roaming host now: it binds `127.0.0.1`
only and is reached over the tailnet, so there is **no cross-VLAN LAN flow to
permit** and no firewall row to add. Its access control is the **tailnet ACL**
(`tag:llm-router → tag:inference-roaming:11434`, human track H2), which is the
policy record for that path — not this matrix. Keep the M4 Pro's LAN reservation
for **SSH only**. Remember the `LocalVLANs` checklist item if any Mac changes
segments.

Known interaction (H5): segmentation tests require Tailscale off on the test
client. If the M4 Pro is that client, `medium` is down for the duration —
expected, and what `fallbacks: {medium: [medium-degraded]}` covers.

## G29 — gaming PC as inference node (deferred)

**Default: DEFER.** Revisit on evidence of a batch workload outgrowing the Macs
(likeliest trigger: an overnight captioning backlog exceeding its window).
Rationale: the box hibernates (an asleep backend fails health checks, so it can't
back any guaranteed alias); 16 GB VRAM caps resident models at the ~12–14 GB class
(duplicates the small role, only adds burst speed); a third node on Windows
expands the secure/monitor/runbook surface.

**Preconditions recorded so a future YES is cheap:** non-sensitive workloads only
(M4 Pro stays the sole sensitive node); engine is llama.cpp CUDA or Ollama-on-
Windows (measure both); wake-on-LAN + scheduled availability, not always-on;
Tailscale-bound; LiteLLM entry only as a health-check failover/burst route, never
primary. **Provisioning is a separate Windows playbook/script — out of scope for
mac-dev-playbook**, which would model the host in the manifest only (same pattern
as the `whisper`/`mlx_hf` entries). Non-LLM (diffusion/image) use is a new gate,
not G29.

## MDP-4 — roaming `medium` transport & gated `small` uplift

`mac-personal` (M4 Pro, `inference_medium`) is a **travelling laptop**. G30 r2/r3
assumed a wired, stationary box and bound `lan_ip`; in the field that host has two
home addresses (wired reservation vs Wi-Fi lease) and sits on public Wi-Fi during
commutes, so the bind is wrong half the time at home and dead everywhere else —
the socket keeps LISTENing on an address the machine no longer holds, which takes
the alias down *and* puts it out of reach of local tools on `127.0.0.1`. `0.0.0.0`
is not an option anywhere: the backend API is unauthenticated.

`mac-headless` (M1 Pro, `inference_small`) is stationary on ServerPhysical and
**does not change transport**.

### Fixed decisions

| ID | Decision |
|---|---|
| **F-MDP4-1** | Transport is a **per-host** property: `inference_transport: lan \| tailnet`, default `lan` in `group_vars/all.yml`, set to `tailnet` in `host_vars/mac-personal.yml`. Topology, not an address — so it is versioned, not in `.env`; and not a connection var, so the inventory-file prohibition holds. |
| **F-MDP4-2** | Under `tailnet`, **every** inference service on that host binds `127.0.0.1`. Not the tailnet IP (it races `tailscaled` at boot and moves local tools off localhost), never `0.0.0.0`. |
| **F-MDP4-3** | `0.0.0.0` / `::` / `[::]` is a **playbook failure** on every host and every transport. Asserted in `tasks/load-local-env.yml`; **no override flag**. |
| **F-MDP4-4** | Caddy is a loopback-only shim: `127.0.0.1:11436`, `admin off`, `auto_https off`, one `reverse_proxy` with `header_up Host localhost:11434` and `flush_interval -1`. Playbook-owned launchd plist, **not** `brew services`. |
| **F-MDP4-5** | The only remote door is `tailscale serve` in **TCP** mode. **Never** `tailscale funnel` — it would publish an unauthenticated inference API to the public internet. Asserted absent. |
| **F-MDP4-6** | Local tools (Raycast, IDE, `ollama` CLI) use `http://127.0.0.1:11434` **directly** — not Caddy, not LiteLLM. A written exception to "reference aliases, never hardware": these are not pipelines, and offline operation is the point. Recorded in `runbooks/ollama.md`. |
| **F-MDP4-7** | The `small` model is **not** changed by this work. Phase C builds the mechanism and the soak test only; flipping `serves_alias: small` is a human act after G-MDP4-3. |
| **F-MDP4-8** | Ports: `ollama_front: 11436` in the manifest registry (11435 is the M1 Pro dev zoo — not reused). Tailnet-side port is `inference_front_tailnet_port`, default `11434`. |

### Gates

| Gate | Default | Trigger to revisit |
|---|---|---|
| **G30 r4** — router→backend transport | **RESOLVED: `lan` for stationary hosts, `tailnet` for roaming hosts.** r2's objections are answered, not ignored: (1) "an extra reachable interface bypasses virtual keys" → the tailnet ACL admits only the router's tag to this port (H2); (2) "tailnet traffic is invisible to the Task 9 matrix" → accepted and documented, and the ACL file is the policy record for this one path. | A second roaming inference host, or the M4 Pro becoming stationary. |
| **G-MDP4-1** — snippet `api_base` host under `tailnet` | **`tailnet_ip`** (raw, per the G33 precedent: no DNS dependency in the inference path; tailnet IPs are stable for the node's lifetime). Override var `tailnet_api_host` allows the MagicDNS name. | The LiteLLM container proves able to resolve MagicDNS **and** the node gets re-keyed often. |
| **G-MDP4-2** — suspend remote serving on battery | **DEFER. Not implemented.** Note only. | Measured battery drain from server-initiated jobs during a commute. |
| **G-MDP4-3** — `small` model uplift on the M1 Pro | **HOLD at `qwen3.5:9b-mlx`** until the Phase C soak passes. Candidates and arithmetic in `runbooks/model-management.md`. | Soak report committed. |

### Notes register (concerns, not relitigation)

- **N1** Caddy may be avoidable: if LiteLLM's per-model `extra_headers: {Host: "localhost:11434"}` survives httpx, `serve → ollama` works with no shim. Untested; cheap to try after H3. If it works, Phase B shrinks to the serve rule — human decision.
- **N2** In TCP mode every request reaches Caddy from `127.0.0.1`, so the access log cannot attribute callers. Attribution lives in the tailnet ACL and LiteLLM's own logs.
- **N3** A future llama.cpp (or the dev zoo, or whisper) on a `tailnet` host binds loopback and is unreachable from the router until it gets its own front route. The emitted snippet renders such entries **commented out** with the reason rather than as live entries the router would dial and hang on. Flag when F6.1.2 lands on the M4 Pro.
- **N4** MLX's documented 32 GB floor (manifest note 5) and a raised GPU wired limit pull in opposite directions on the M1 Pro. A soak report must record which backend (`mlx` vs GGML) actually served the candidate.
- **N5** `main` still derives addresses from `.env`; MDP-3's inventory-as-source-of-truth refactor is not on `main`. This work is written against `main` as it is. If MDP-3 lands first, the transport-aware address resolution rebases onto its address source; nothing else moves.
