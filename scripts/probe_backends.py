#!/usr/bin/env python3
"""Backend liveness probe for the Mac inference nodes (MDP-2 T18, F11).

This answers "backend down vs. router broken" — the BACKEND half of the two-layer
test model. Alias-level end-to-end testing is NOT here; it is canonical in the
router-side `smoke-test.sh` (Brief 1 router package). This script never touches
the router and never runs during provisioning.

It reads per-host LAN IPs from the shared `.env` (the same parser Ansible uses),
figures out which host has which inference role from the `inventory`, derives the
port set from `model_manifest.yml` (11434 per ollama role host, 8082 for the
whisper/transcribe entry, any llamacpp ports), then does a GET liveness probe of
each backend. A probe PASSES if the server answers with any HTTP status (it is
up); it FAILS on connection refused / timeout (it is down). Latency is reported,
never asserted (G19 budgets live in Brief 12). Exit is non-zero on any unexpected
state — down when it should be up, or up under --expect-down.

Stdlib only; no network access unless you run it live (the human's call).
"""

import argparse
import os
import socket
import sys
import time
import urllib.error
import urllib.request

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import local_env  # noqa: E402  (shared .env parser, F11)

# Per-service liveness path. A GET here returning ANY HTTP status means "up".
SERVICE_PATHS = {
    "ollama": "/api/tags",
    "transcribe": "/",          # whisper-server root (no auth; reachability only)
    "llamacpp": "/v1/models",
}
OLLAMA_PORT = 11434


# --------------------------------------------------------------------------- #
# Deriving what to probe
# --------------------------------------------------------------------------- #
def parse_inventory_roles(text):
    """Return {'small': [host, ...], 'medium': [host, ...]} from an ini inventory.

    Only the inference-role groups matter here. Host aliases are the first token
    of each non-comment line under `[inference_small]` / `[inference_medium]`.
    """
    roles = {"small": [], "medium": []}
    section = None
    group_of = {"inference_small": "small", "inference_medium": "medium"}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        role = group_of.get(section)
        if role is not None:
            host = line.split()[0]
            if host:
                roles[role].append(host)
    return roles


def ports_for_role(models, role):
    """List of (port, service, path) a host in `role` should be serving."""
    in_role = [m for m in models
               if str(m.get("role", "")).lower() in (role, "either")]
    out = []
    if any(str(m.get("engine", "")).lower() == "ollama" for m in in_role):
        out.append((OLLAMA_PORT, "ollama", SERVICE_PATHS["ollama"]))
    for m in in_role:
        engine = str(m.get("engine", "")).lower()
        port = m.get("port")
        if engine == "whisper" and port:
            out.append((port, "transcribe", SERVICE_PATHS["transcribe"]))
        elif engine == "llamacpp" and port:
            out.append((port, "llamacpp", SERVICE_PATHS["llamacpp"]))
    return out


def host_key(host):
    """Inventory alias -> .env key prefix (mac-headless -> MAC_HEADLESS)."""
    return "".join(c if c.isalnum() else "_" for c in host.upper())


def build_targets(env, roles, models):
    """Build the flat list of probe targets from env + role map + manifest."""
    targets = []
    for role, hosts in roles.items():
        port_set = ports_for_role(models, role)
        for host in hosts:
            key = host_key(host) + "_LAN_IP"
            lan_ip = env.get(key)
            for port, service, path in port_set:
                targets.append({
                    "host": host, "role": role, "lan_ip": lan_ip,
                    "port": port, "service": service, "path": path,
                    "env_key": key,
                })
    return targets


# --------------------------------------------------------------------------- #
# Probing
# --------------------------------------------------------------------------- #
def probe(target, timeout=10.0):
    """GET the target's liveness path. ok=True if the server answered at all."""
    result = dict(target)
    if not target.get("lan_ip"):
        result.update(ok=False, status=None, latency_ms=None,
                      error="no %s in .env" % target["env_key"])
        return result
    url = "http://%s:%s%s" % (target["lan_ip"], target["port"], target["path"])
    start = time.perf_counter()
    try:
        resp = urllib.request.urlopen(url, timeout=timeout)  # noqa: S310 (http, liveness)
        status = getattr(resp, "status", None) or resp.getcode()
        resp.close()
        ok, error = True, None
    except urllib.error.HTTPError as exc:
        status, ok, error = exc.code, True, None      # answered (non-2xx) => up
    except (urllib.error.URLError, socket.timeout, OSError) as exc:
        status, ok, error = None, False, str(getattr(exc, "reason", exc))
    result.update(ok=ok, status=status,
                  latency_ms=round((time.perf_counter() - start) * 1000, 1),
                  error=error)
    return result


def run(targets, timeout=10.0, expect_down=None):
    """Probe every target and mark pass/fail against expectations."""
    results = []
    for t in targets:
        r = probe(t, timeout)
        expected_down = (r["host"] == expect_down)
        r["expected"] = "down" if expected_down else "up"
        r["pass"] = (not r["ok"]) if expected_down else r["ok"]
        results.append(r)
    return results, all(r["pass"] for r in results)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _render(results):
    lines = ["%-14s %-11s %-6s %-9s %-4s %s"
             % ("HOST", "SERVICE", "PORT", "LATENCY", "EXP", "RESULT")]
    for r in results:
        lat = "---" if r["latency_ms"] is None else "%sms" % r["latency_ms"]
        verdict = "PASS" if r["pass"] else "FAIL"
        detail = "" if r["ok"] and not r["error"] else \
            (" (%s)" % (r["error"] or ("status %s" % r["status"])))
        lines.append("%-14s %-11s %-6s %-9s %-4s %s%s"
                     % (r["host"], r["service"], r["port"], lat,
                        r["expected"], verdict, detail))
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--env", default=os.path.join(_REPO, ".env"))
    p.add_argument("--inventory", default=os.path.join(_REPO, "inventory"))
    p.add_argument("--manifest", default=os.path.join(_REPO, "model_manifest.yml"))
    p.add_argument("--only", help="probe only this inventory host")
    p.add_argument("--expect-down", help="this host's probes must FAIL; all others PASS")
    p.add_argument("--timeout", type=float, default=10.0)
    args = p.parse_args(argv)

    import plan_model_pulls  # local import; shares scripts/ on sys.path
    env = local_env.load_env(args.env)
    try:
        with open(args.inventory) as fh:
            roles = parse_inventory_roles(fh.read())
    except FileNotFoundError:
        roles = {"small": [], "medium": []}
    models = plan_model_pulls.load_manifest(args.manifest)

    targets = build_targets(env, roles, models)
    if args.only:
        targets = [t for t in targets if t["host"] == args.only]

    if not targets:
        sys.stderr.write("no inference backends to probe "
                         "(add hosts to inference_small/inference_medium and .env)\n")
        return 0

    results, ok = run(targets, timeout=args.timeout, expect_down=args.expect_down)
    sys.stdout.write(_render(results) + "\n")
    passed = sum(1 for r in results if r["pass"])
    sys.stdout.write("Summary: %d/%d probes as expected. %s\n"
                     % (passed, len(results), "OK" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
