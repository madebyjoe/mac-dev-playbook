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
current `model_manifest.yml` is **SYNTHETIC** (Standing Rule 2); replace it via
G25. After a live pull, a verify step compares actual vs declared size and warns on
>20% divergence so the manifest can be corrected.

## Engine policy (F6.1, Amendment 1)

**Ollama is the primary engine** on all Mac inference nodes (its Apple Silicon
backend is MLX — fastest for the whole `small` role). **llama.cpp** (`engine:
llamacpp`) is the ratified **second** engine for HF-only distributions, custom
quants, and long-context `medium` work where MLX's TTFT penalty is measured to be
unacceptable; its pull/serve support is **planned as T14** and not yet
implemented. The **v1 planner is ollama-only**: it pulls/verifies `ollama`
entries and *skips* everything else with a notice — `llamacpp` entries appear
under a distinct **"pending T14"** sub-heading (visible as planned work), while
`whisper` / `mlx_hf` / `none` appear under "engine not implemented". No engine
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

## Rollback

The system is additive and non-destructive — see `docs/rollback-notes.md` (T9–T11).
Reverting the commits restores the prior state; already-pulled models are never
removed by any code path.
