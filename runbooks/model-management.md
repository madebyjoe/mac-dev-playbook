# Runbook: storage-aware model management

**What it is.** `model_manifest.yml` is the single source of truth for which models
exist; `scripts/plan_model_pulls.py` decides which of them a node should pull, and
`tasks/pull-models.yml` runs that plan. The manifest is a list under `models:`,
one mapping per entry with fields `name` (engine model tag), `engine` (`ollama`
only in v1 — other engines are parsed and **skipped**, not errored, F6), `size_gb`
(**declared** on-disk size, trusted for fit decisions), `role`
(`small`/`medium`/`either`), `priority` (integer, **lower pulls first**), and
`notes`. The planner takes the node's role (from its inference-role group) and,
after subtracting `reserve_floor_gb` (G26, default 100) from the volume's free
space, greedily selects by priority the ollama entries that fit. Models already on
disk cost zero and are kept. **Nothing is ever deleted**: models that do not fit
are reported as *deferred* (with the shortfall), and when a higher-priority model
cannot fit while a lower-priority one is present, the planner prints an *eviction
recommendation only* — the human decides whether to `ollama rm` anything. The
current `model_manifest.yml` is the **G25-ratified** manifest (see
`docs/decision-gates.md`); it still carries "estimate — verify" flags on some
entries. After a live pull, a verify step compares actual vs declared size and
warns on >20% divergence so those estimates can be corrected.

## Engine policy (F6.1, Amendment 1)

**Ollama is the primary engine** on all Mac inference nodes (its Apple Silicon
backend is MLX — fastest for the whole `small` role). **llama.cpp** (`engine:
llamacpp`) is the ratified **second** engine for HF-only distributions, custom
quants, and long-context `medium` work where MLX's TTFT penalty is measured to be
unacceptable; it is **implemented as of T14** (pull via `huggingface-cli`, per-model
`llama-server` launchd services — see `runbooks/llamacpp.md`). The planner now
plans **both** engines against the shared disk budget. `whisper` / `mlx_hf` /
`none` remain unimplemented and appear under "engine not implemented". No engine
choice is a category error to relitigate — see the rationale in the amendment.

## Commands

```sh
# See the plan for a role without touching anything (human-readable).
python3 scripts/plan_model_pulls.py --role small --reserve-floor-gb 100

# Machine-readable plan (what the playbook consumes).
python3 scripts/plan_model_pulls.py --role medium --format json

# Reconcile declared vs actual sizes of present models.
python3 scripts/plan_model_pulls.py --role small --verify

# Dry plan via the playbook (default: no pulls). Add -e model_pull_dry_run=false
# to actually pull on an inference node.
ansible-playbook main.yml --limit headless --tags models

# Run the unit tests (no ollama required).
python3 -m unittest discover -s tests
```

## Handling eviction recommendations (human)

The planner only *recommends*. To act on one, evict the named lower-priority model
yourself, then re-run the plan:

```sh
ollama rm <model-name>
python3 scripts/plan_model_pulls.py --role small   # the deferred model should now fit
```

## Bare aliases: `serves_alias` and `also_serves` (schema v1.3)

`serves_alias` marks the ONE entry per role that backs a bare LiteLLM alias
(`small`, `medium`, `embed`, `transcribe`). `also_serves` (v1.3) lets that same
entry publish **additional** bare aliases for the **same backend and the same
tag** — it is an alias, not a second model.

It exists so the router can name a fallback without pretending the fallback is a
different model. The shipped case: the `small` entry also publishes
`medium-degraded`, so the router can declare

```yaml
router_settings:
  fallbacks: {medium: [medium-degraded]}
```

and keep answering `medium` when the roaming M4 Pro is in a tunnel. That is
useful **today**, independent of any uplift: whether `small` stays at 9B or moves
to a bigger candidate later, `medium-degraded` keeps pointing at whatever backs
`small`.

Both fields live in the **same namespace**. `plan_model_pulls.py --lint`
(which `tasks/litellm-artifact.yml` runs before rendering) enforces uniqueness
across `serves_alias` ∪ `also_serves` per role, and rejects a `-cloud` name in
either — a `-cloud` *alias* routes inference off-LAN exactly as effectively as a
`-cloud` pull tag, and it is the name a human actually types.

## The `small` uplift (G-MDP4-3) — candidates, arithmetic, and the procedure

**Status: HOLD at `qwen3.5:9b-mlx`.** The mechanism ships; the model does not
change until a soak passes and a human ratifies it (F-MDP4-7).

**Why this is gated.** Today's pinned floor on the M1 Pro (32 GB unified) is
8.12 GB (`small`) + ~2.5 GB (`embed`) + 1.6 GB (whisper-server) ≈ **12 GB**, plus
one 8.5 GB dev-zoo slot. `medium` on the M4 Pro is `qwen3.6:27b-mlx` (20 GB
dense). For `small` to be a credible *degraded* `medium`:

| Candidate | Weights | Floor with embed + whisper | Verdict |
|---|---|---|---|
| `qwen3.5:9b-mlx` (current) | 8.1 GB | ~12 GB | Safe. Weak as a `medium` stand-in. **DEFAULT.** |
| `qwen3.6:35b-mlx` (35B-A3B MoE) | 22 GB | ~26 GB + KV | Same family as `medium`; ~3B active so decode is *faster* than the 9B dense. Needs the GPU wired limit raised (default ≈ 21 GB on a 32 GB Mac), **dev zoo disabled**, and it lives right next to the failure mode already seen on this box (`Compute error.` 500s from `embed` = Metal allocator). |
| `gemma4:26b-mlx` (MoE, 4B active) | ~18 GB | ~22 GB + KV | More headroom; different family/template from `medium`, so prompts tuned for Qwen behave differently. |
| `qwen3.6:27b-mlx` (dense) | 20 GB | ~24 GB + KV | **Rejected.** ~9 tok/s on M1 Pro bandwidth; every `small` consumer (Brief 2 batch, Brief 12 voice fallback) gets slower. |

### Measure first

```sh
# 20 minutes of CONCURRENT chat + embeddings + transcribe, sampling pressure,
# swap and residency. Read-only: pulls nothing, evicts nothing, changes no alias.
scripts/soak_small_candidate.sh qwen3.6:35b-mlx 1200 http://<host>:11434
```

**Pass =** zero non-200 on `embed` **and** `transcribe` · swap delta < 1 GB ·
pressure never `critical` · both models still in `ollama ps` at the end. The
script decides all of these and writes `soak-<tag>-<date>.md`.

**TTFT is deliberately not pass/fail** — the acceptable budget is a human call
against G19 voice latency, made by reading the latency table.

Record which backend actually served it (N4): the M1 Pro sits exactly at MLX's
documented 32 GB floor and falls back to GGML **silently**.

```sh
grep -iE 'mlx|ggml|metal' ~/Library/Logs/ollama/ollama.log | tail -20
```

### The flip (human, one variable at a time)

A soak PASS is evidence, not a decision. Do these **in this order**, and do not
combine them — if two things change at once and the box misbehaves, you have
learned nothing about which one did it.

1. **Raise the GPU wired limit. Reboot. Do nothing else.**
   ```yaml
   # host_vars/mac-headless.yml
   gpu_wired_limit_mb: 26624        # 26 GB of 32; the task asserts <= total - 5 GB
   ```
   ```sh
   ansible-playbook main.yml --limit headless --tags gpu-wired-limit -K
   sudo reboot
   ```
   The sysctl does **not** persist on its own — that is what the LaunchDaemon is
   for. After the reboot confirm it stuck:
   `sysctl -n iogpu.wired_limit_mb`.

2. **Disable the dev zoo** on that host, if the candidate needs the slot:
   ```yaml
   # group_vars/inference_small.yml
   ollama_dev_enabled: false
   ```
   The zoo exists because ad-hoc experimentation was evicting pipeline models —
   re-read the top of `runbooks/ollama.md` before deciding you are fine without
   it.

3. **Soak the candidate** (above). Commit the report.

4. **Only then move the alias.** In `model_manifest.yml`, move `serves_alias:
   small` **and** `also_serves: [medium-degraded]` from the 9B entry to the
   candidate, and set the candidate's `tier: pipeline`.

5. **Rewrite the slot-budget arithmetic comment** in
   `group_vars/inference_small.yml`. It currently documents a ~10.6 GB pipeline
   floor and two slots; a 22 GB candidate makes that comment a lie, and the next
   person will trust it.

6. **Amend the manifest header rule with a dated supersession.** The header says
   *"nothing over ~10GB weights on the M1 Pro"*. Do not delete it — add the date,
   the new ceiling, and the soak report that justifies it, so the reasoning
   survives.

7. **Re-validate the Brief 2 classification prompts** against synthetic fixtures.
   A different model family (Gemma vs Qwen) has a different chat template and
   different refusal behaviour; prompts tuned for one do not transfer silently.

8. **Re-render and re-apply the router snippet**, and re-run the router-side
   `smoke-test.sh` at least three times — check 5 (`embed`) is deliberately
   ordered *after* the chat checks because `embed` in isolation passes 6/6 and
   would hide exactly the fault this uplift risks.

**Rolling back the flip:** move `serves_alias`/`also_serves` back, re-render, and
re-apply. The old model is still on disk — nothing in this repo ever deletes one.
Lower `gpu_wired_limit_mb` (or remove it and delete
`/Library/LaunchDaemons/com.madebyjoe.gpu-wired-limit.plist`, then reboot).

## Rollback

The system is additive and non-destructive — see `docs/rollback-notes.md` (T9–T11,
and MDP-4 for the transport and GPU-limit work). Reverting the commits restores
the prior state; already-pulled models are never removed by any code path.
