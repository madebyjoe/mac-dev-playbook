#!/usr/bin/env python3
"""Storage-aware Ollama model-pull planner (F5/F6).

Deterministic, stdlib-only, and NON-DESTRUCTIVE by construction: this tool only
ever computes and prints a plan. It NEVER pulls and NEVER deletes. Pulls are
performed by tasks/pull-models.yml from the JSON plan; evictions are printed as
recommendations for the human only.

Selection: from the manifest, take entries whose role matches the node role
(`either` always qualifies) and whose engine is implemented (`ollama` in v1).
Models already present on disk cost zero and are kept. The remaining candidates
are selected greedily in priority order (lower priority integer first, ties by
name), pulling each whose declared size fits within `free - reserve_floor`.
Non-ollama engines are reported as skipped; candidates that do not fit are
reported as deferred with their shortfall.

Free space comes from os.statvfs on the volume containing --models-dir, unless
--free-gb overrides it (used by tests so scenarios are deterministic). Present
models come from `ollama list`, unless the OLLAMA_LIST_OUTPUT env var supplies
canned output (used by tests so ollama need not be installed).

Sizes are treated in binary GB (GiB, 1024**3 bytes) throughout; the 20% verify
tolerance makes the GB-vs-GiB labelling difference immaterial.
"""

import argparse
import json
import os
import subprocess
import sys

BYTES_PER_GB = 1024 ** 3
VERIFY_TOLERANCE = 0.20


# --------------------------------------------------------------------------- #
# Minimal YAML loader for the manifest subset: a single `models:` list whose
# entries are flat mappings, written either BLOCK style
#   - name: foo
#     engine: ollama
# or FLOW style on one line
#   - { name: foo, engine: ollama, notes: "commas, and colons: ok inside quotes" }
# Stdlib has no YAML parser and the brief forbids pip deps, so we parse exactly
# the documented schema. Anything richer (anchors, nested maps, multi-line
# scalars) is out of scope by design.
# --------------------------------------------------------------------------- #
def _split_top_level_commas(text):
    """Split on commas that are not inside single/double quotes."""
    parts, buf, quote = [], [], None
    for ch in text:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch == ",":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return parts


def _parse_flow_mapping(text):
    """Parse a one-line flow mapping `{ k: v, k: v, ... }` into a dict."""
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start + 1:end]
    out = {}
    for part in _split_top_level_commas(text):
        part = part.strip()
        if not part:
            continue
        k, v = _split_kv(part)
        out[k] = v
    return out


def _split_kv(text):
    """Split 'key: value' on the first colon that is followed by space/EOL.

    Model names contain colons (`synthetic-small-a:7b`), so a naive split breaks.
    """
    idx = None
    for i, ch in enumerate(text):
        if ch == ":" and (i + 1 == len(text) or text[i + 1] in " \t"):
            idx = i
            break
    if idx is None:
        return text.strip(), None
    key = text[:idx].strip()
    val = text[idx + 1:].strip()
    return key, _coerce(val)


def _coerce(val):
    if val == "" or val is None:
        return None
    if (val[0] == '"' and val[-1] == '"') or (val[0] == "'" and val[-1] == "'"):
        return val[1:-1]
    # strip a trailing inline comment on unquoted scalars
    if " #" in val:
        val = val.split(" #", 1)[0].strip()
    low = val.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(val)
    except ValueError:
        pass
    try:
        return float(val)
    except ValueError:
        pass
    return val


def load_manifest(path):
    """Parse the manifest file into a list of model dicts."""
    with open(path) as fh:
        raw_lines = fh.readlines()
    models = []
    current = None
    in_models = False
    for raw in raw_lines:
        line = raw.rstrip("\n")
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped == "---":
            continue
        # Top-level key (no indentation, not a list item).
        if not line[0].isspace() and not stripped.startswith("-"):
            key, _ = _split_kv(stripped)
            in_models = key == "models"
            current = None
            continue
        if not in_models:
            continue
        body = line.lstrip(" ")
        if body.startswith("- "):
            item = body[2:].strip()
            if item.startswith("{"):          # flow-style: complete on one line
                models.append(_parse_flow_mapping(item))
                current = None
            else:                             # block-style: first key on this line
                current = {}
                models.append(current)
                if item:
                    k, v = _split_kv(item)
                    current[k] = v
        elif current is not None:             # continuation of a block-style entry
            k, v = _split_kv(body)
            current[k] = v
    return models


# --------------------------------------------------------------------------- #
# Environment probes
# --------------------------------------------------------------------------- #
def free_gb_for(models_dir):
    """Free GB available to the user on the volume holding models_dir."""
    probe = models_dir
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    if not probe or not os.path.exists(probe):
        probe = os.path.expanduser("~")
    st = os.statvfs(probe)
    return (st.f_bavail * st.f_frsize) / BYTES_PER_GB


def _parse_size_to_gb(text):
    """Parse an `ollama list` SIZE like '4.9 GB' / '512 MB' into GB (float)."""
    parts = text.split()
    if not parts:
        return None
    try:
        num = float(parts[0])
    except ValueError:
        return None
    unit = (parts[1].upper() if len(parts) > 1 else "GB")
    factor = {"KB": 1 / 1024 ** 2, "MB": 1 / 1024, "GB": 1.0, "TB": 1024.0}.get(unit, 1.0)
    return num * factor


def ollama_list(ollama_bin="ollama"):
    """Return {model_name: size_gb_or_None} of present models.

    Uses OLLAMA_LIST_OUTPUT (raw `ollama list` text) if set, so tests need not
    have ollama installed. On any subprocess failure, returns {} and warns.
    """
    canned = os.environ.get("OLLAMA_LIST_OUTPUT")
    if canned is not None:
        text = canned
    else:
        try:
            text = subprocess.run(
                [ollama_bin, "list"],
                check=True, capture_output=True, text=True,
            ).stdout
        except (OSError, subprocess.CalledProcessError) as exc:
            sys.stderr.write(
                "warning: could not run `%s list` (%s); "
                "assuming no models present\n" % (ollama_bin, exc)
            )
            return {}
    present = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        if line.split()[0].upper() == "NAME":  # header
            continue
        cols = line.split()
        name = cols[0]
        # SIZE is columns [2],[3] like '4.9 GB' in default `ollama list` output.
        size = None
        if len(cols) >= 4:
            size = _parse_size_to_gb(cols[2] + " " + cols[3])
        present[name] = size
    return present


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #
def build_plan(models, role, free_gb, reserve_floor_gb, present):
    """Pure planning function — no I/O. Returns the plan dict."""
    budget = free_gb - reserve_floor_gb

    skipped, candidates = [], []
    for m in models:
        engine = str(m.get("engine", "")).lower()
        m_role = str(m.get("role", "")).lower()
        if m_role not in (role, "either"):
            continue
        if engine != "ollama":
            # F6.1: llamacpp is the ratified second engine (planned as T14), so
            # surface it separately from genuinely-unimplemented engines
            # (whisper / mlx_hf / none) rather than burying it as noise.
            if engine == "llamacpp":
                skipped.append({
                    "name": m.get("name"),
                    "engine": m.get("engine"),
                    "status": "pending_t14",
                    "reason": "pending T14 (llama.cpp engine support)",
                })
            else:
                skipped.append({
                    "name": m.get("name"),
                    "engine": m.get("engine"),
                    "status": "not_implemented",
                    "reason": "engine not implemented (v1 is ollama-only)",
                })
            continue
        candidates.append(m)

    candidates.sort(key=lambda x: (x.get("priority", 0), str(x.get("name"))))

    pull, present_kept, deferred = [], [], []
    remaining = budget
    for m in candidates:
        name = m.get("name")
        size = float(m.get("size_gb", 0))
        entry = {
            "name": name, "engine": "ollama",
            "size_gb": m.get("size_gb"), "priority": m.get("priority"),
        }
        if name in present:
            present_kept.append(entry)  # already on disk: zero cost, kept
            continue
        if size <= remaining:
            pull.append(entry)
            remaining -= size
        else:
            short = round(size - remaining, 3)
            d = dict(entry)
            d["shortfall_gb"] = short
            deferred.append(d)

    evictions = _eviction_recommendations(deferred, present_kept, present, budget)

    return {
        "role": role,
        "free_gb": round(free_gb, 3),
        "reserve_floor_gb": reserve_floor_gb,
        "budget_gb": round(budget, 3),
        "remaining_gb": round(remaining, 3),
        "pull": pull,
        "present": present_kept,
        "deferred": deferred,
        "skipped": skipped,
        "eviction_recommendations": evictions,
    }


def _eviction_recommendations(deferred, present_kept, present, budget):
    """For each deferred model, suggest (never perform) evicting lower-priority
    present models that would free enough space. Advisory only (F5)."""
    if not deferred:
        return []
    # Present models we know the priority of, from the manifest match.
    known = {e["name"]: e for e in present_kept if e.get("priority") is not None}
    recs = []
    for d in deferred:
        d_pri = d.get("priority")
        # lower priority == higher integer; only those are eviction candidates
        cands = []
        for name, entry in known.items():
            if d_pri is not None and entry["priority"] is not None \
                    and entry["priority"] > d_pri:
                size = present.get(name)
                cands.append({"name": name, "size_gb": size,
                              "priority": entry["priority"]})
        cands.sort(key=lambda c: -(c["priority"] or 0))
        if cands:
            recs.append({
                "deferred": d["name"],
                "shortfall_gb": d.get("shortfall_gb"),
                "evict_candidates": cands,
                "note": "RECOMMENDATION ONLY — the planner never deletes models.",
            })
    return recs


# --------------------------------------------------------------------------- #
# Verification (post-pull) — compare declared size_gb vs actual on disk
# --------------------------------------------------------------------------- #
def build_verification(models, present):
    """Return divergences where |actual-declared|/declared > tolerance."""
    diverged = []
    by_name = {m.get("name"): m for m in models if str(m.get("engine", "")).lower() == "ollama"}
    for name, actual in present.items():
        m = by_name.get(name)
        if m is None or actual is None:
            continue
        declared = float(m.get("size_gb", 0))
        if declared <= 0:
            continue
        ratio = abs(actual - declared) / declared
        if ratio > VERIFY_TOLERANCE:
            diverged.append({
                "name": name,
                "declared_gb": m.get("size_gb"),
                "actual_gb": round(actual, 2),
                "divergence_pct": round(ratio * 100, 1),
            })
    return {"tolerance_pct": VERIFY_TOLERANCE * 100, "diverged": diverged}


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def render_human(plan):
    L = []
    L.append("Model pull plan — role=%s" % plan["role"])
    L.append("  free=%.1f GB  reserve_floor=%s GB  budget=%.1f GB  remaining=%.1f GB"
             % (plan["free_gb"], plan["reserve_floor_gb"], plan["budget_gb"], plan["remaining_gb"]))
    L.append("")
    L.append("PULL (%d):" % len(plan["pull"]))
    for e in plan["pull"]:
        L.append("  + %-38s %s GB  (priority %s)" % (e["name"], e["size_gb"], e["priority"]))
    L.append("PRESENT / kept (%d):" % len(plan["present"]))
    for e in plan["present"]:
        L.append("  = %-38s (already on disk, zero cost)" % e["name"])
    L.append("DEFERRED / insufficient space (%d):" % len(plan["deferred"]))
    for e in plan["deferred"]:
        L.append("  ! %-38s %s GB  short by %s GB  (priority %s)"
                 % (e["name"], e["size_gb"], e["shortfall_gb"], e["priority"]))
    not_impl = [e for e in plan["skipped"] if e.get("status") != "pending_t14"]
    pending = [e for e in plan["skipped"] if e.get("status") == "pending_t14"]
    L.append("SKIPPED / engine not implemented (%d):" % len(not_impl))
    for e in not_impl:
        L.append("  ~ %-38s engine=%s" % (e["name"], e["engine"]))
    if pending:
        L.append("SKIPPED / pending T14 — llama.cpp (%d):" % len(pending))
        for e in pending:
            L.append("  … %-38s engine=%s" % (e["name"], e["engine"]))
    if plan["eviction_recommendations"]:
        L.append("EVICTION RECOMMENDATIONS (advisory only — nothing is deleted):")
        for r in plan["eviction_recommendations"]:
            names = ", ".join(c["name"] for c in r["evict_candidates"])
            L.append("  ? to fit %s (short %s GB) consider evicting: %s"
                     % (r["deferred"], r["shortfall_gb"], names))
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    default_manifest = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "model_manifest.yml")
    p.add_argument("--manifest", default=default_manifest)
    p.add_argument("--role", required=True, choices=["small", "medium"])
    p.add_argument("--models-dir", default=os.path.expanduser("~/.ollama/models"))
    p.add_argument("--reserve-floor-gb", type=float, default=100.0)
    p.add_argument("--free-gb", type=float, default=None,
                   help="override free space (GB) instead of probing the volume; for tests")
    p.add_argument("--ollama-bin", default="ollama")
    p.add_argument("--format", choices=["human", "json"], default="human",
                   help="stdout format; the human-readable plan always goes to stderr too")
    p.add_argument("--verify", action="store_true",
                   help="compare declared vs actual sizes of present models instead of planning")
    p.add_argument("--dry-run", action="store_true",
                   help="explicit no-op flag; the planner never pulls or deletes regardless")
    args = p.parse_args(argv)

    models = load_manifest(args.manifest)
    present = ollama_list(args.ollama_bin)

    if args.verify:
        result = build_verification(models, present)
        sys.stdout.write(json.dumps(result, indent=2) + "\n")
        for d in result["diverged"]:
            sys.stderr.write(
                "warning: %s declared %s GB but actual %s GB (%.1f%% divergence)\n"
                % (d["name"], d["declared_gb"], d["actual_gb"], d["divergence_pct"]))
        return 0

    free = args.free_gb if args.free_gb is not None else free_gb_for(args.models_dir)
    plan = build_plan(models, args.role, free, args.reserve_floor_gb, present)

    human = render_human(plan)
    sys.stderr.write(human + "\n")
    if args.format == "json":
        sys.stdout.write(json.dumps(plan, indent=2) + "\n")
    else:
        sys.stdout.write(human + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
