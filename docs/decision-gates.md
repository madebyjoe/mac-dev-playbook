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
| **G30** — router→backend transport | **RESOLVED = LAN (r2)** | Backends bind `lan_ip`; the snippet `api_base` uses `lan_ip`. The router at `:4000` is the only enforcement door; unauthenticated backend ports must not be reachable off the LAN. Tailscale on the Macs is management-only. **Roaming caveat:** a Mac off-LAN stops serving and the router 503s its aliases (correct, drill D10). See below for the Task 9a firewall consequence (r3). |
| **G31** — service definitions for :8082 / :8084 | **RESOLVED (r3)** | Neither service was ever deployed. **G31a transcribe** = build whisper-server (`whisper-cpp`), bind `lan_ip:8082`, no auth (T17). **G31b embed** = consolidate onto ollama `:11434`; the standalone `:8084` server is retired/unbuilt — **port 8084 removed everywhere**. |
| **G32** — smoke-test virtual key | **Router-side, human** | `vk-smoke`: low-privilege LiteLLM virtual key allow-listing exactly `small`, `medium`, `transcribe`, `embed` — never `caption-unfiltered`. Created on the router; value goes only into `.env`. The negative-auth assertion lives in the router-side `smoke-test.sh`, not this repo. |
| **G33** — snippet `api_base` naming | **RESOLVED = raw `lan_ip` (v1)** | Backed by static DHCP reservations. Unbound names (`m1pro.…internal`) survive readdressing but add a DNS dependency to the inference path — flagged as a later amendment, not implemented here. |

### G30 firewall consequence — Task 9a rows (r3, doc reference only)

The executor never touches firewall config. Recorded so the Task 8/9 work stays in
sync: the **M1 Pro has moved to the ServerPhysical VLAN**, so all Unraid → M1 Pro
flows (`11434`, `8082`) are now **intra-VLAN — no rows required**. The **sole
surviving cross-VLAN row** is `Unraid (LiteLLM) → M4PRO_HOST : 11434/tcp` (single
direction, justification: LiteLLM backend reach). It works today under the
allow-to-any pattern; that one row must land before Task 9 enforcement cutover.
Remember the `LocalVLANs` checklist item if any Mac changes segments.

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
