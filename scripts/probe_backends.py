#!/usr/bin/env python3
"""Backend liveness probe for the Mac inference nodes (MDP-2 T18, F11).

This answers "backend down vs. router broken" — the BACKEND half of the two-layer
test model. Alias-level end-to-end testing is NOT here; it is canonical in the
router-side `smoke-test.sh` (Brief 1 router package). This script never touches
the router and never runs during provisioning.

It reads per-host addresses from the shared `.env` (the same parser Ansible uses),
figures out which host has which inference role from the `inventory`, reads each
host's TRANSPORT from `host_vars/<host>.yml`, derives the port set from
`model_manifest.yml`, then does a GET liveness probe of each backend.

Target selection mirrors `templates/litellm-snippet.yml.j2` exactly, because the
point of this probe is to test what the ROUTER will dial (MDP-4, G30 r4):

  lan     — probe the host's `*_LAN_IP` on 11434 per ollama role host, 11435 for
            the second "dev zoo" ollama instance where the role has `tier: dev`
            entries, 8082 for the whisper/transcribe entry, and any llamacpp
            ports. Unchanged behaviour.
  tailnet — probe the host's `*_TAILNET_IP` on the fronted port only. On a
            roaming host every service binds 127.0.0.1 (F-MDP4-2) and
            `tailscale serve` fronts the ollama pipeline port alone, so the dev
            zoo / whisper / llamacpp ports have no remote route at all (N3).
            Probing them from here would report "down" for services that are
            running perfectly well, so they are listed as unrouted instead.

A probe PASSES if the server answers with any HTTP status (it is
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
# Port `tailscale serve` publishes on the tailnet for a roaming host. Mirrors
# `inference_front_tailnet_port` in group_vars/all.yml.
FRONT_TAILNET_PORT = 11434


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


def host_transport(host, host_vars_dir):
    """Return 'tailnet' or 'lan' for `host`, from host_vars/<host>.yml.

    Transport is topology, so it lives in a versioned host_vars file rather than
    in `.env` (F-MDP4-1). Only one scalar key is needed, so this does a line scan
    rather than pulling in a YAML dependency — same tactic as the manifest
    parser. Anything unreadable or unset means the `lan` default, which is also
    what group_vars/all.yml says.
    """
    path = os.path.join(host_vars_dir, "%s.yml" % host)
    try:
        with open(path) as fh:
            text = fh.read()
    except OSError:
        return "lan"
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        if key.strip() == "inference_transport":
            value = value.split("#", 1)[0].strip().strip("\"'")
            if value in ("lan", "tailnet"):
                return value
    return "lan"


def load_transports(hosts, host_vars_dir):
    """{host: transport} for every host named in the inventory role groups."""
    return {h: host_transport(h, host_vars_dir) for h in hosts}


def ports_for_role(models, role, dev_port=None, transport="lan",
                   front_tailnet_port=FRONT_TAILNET_PORT):
    """(probed, unrouted) for a host in `role` on `transport`.

    `probed` is a list of (port, service, path) reachable from the control node;
    `unrouted` is a list of (port, service) that exist on the host but have no
    remote route and so must NOT be probed (see the module docstring).

    `dev_port` is the second ("dev zoo") ollama instance from the manifest
    `ports:` registry. It is only probed when this role actually has entries
    explicitly marked `tier: dev` — a host with no dev zoo runs one instance and
    must not be reported down for a port it was never meant to serve.
    """
    in_role = [m for m in models
               if str(m.get("role", "")).lower() in (role, "either")]
    out, unrouted = [], []
    has_ollama = any(str(m.get("engine", "")).lower() == "ollama" for m in in_role)
    tailnet = transport == "tailnet"

    if has_ollama:
        # Under `tailnet` the pipeline instance is reached through the serve/Caddy
        # front on the tailnet port; under `lan` it is dialled directly.
        out.append((front_tailnet_port if tailnet else OLLAMA_PORT,
                    "ollama-front" if tailnet else "ollama",
                    SERVICE_PATHS["ollama"]))

    def _add(port, service, path):
        if not port:
            return
        if tailnet:
            unrouted.append((port, service))
        else:
            out.append((port, service, path))

    if dev_port and any(str(m.get("engine", "")).lower() == "ollama"
                        and str(m.get("tier", "")).lower() == "dev"
                        for m in in_role):
        _add(dev_port, "ollama-dev", SERVICE_PATHS["ollama"])
    for m in in_role:
        engine = str(m.get("engine", "")).lower()
        port = m.get("port")
        if engine == "whisper":
            _add(port, "transcribe", SERVICE_PATHS["transcribe"])
        elif engine == "llamacpp":
            _add(port, "llamacpp", SERVICE_PATHS["llamacpp"])
    return out, unrouted


def host_key(host):
    """Inventory alias -> .env key prefix (mac-headless -> MAC_HEADLESS)."""
    return "".join(c if c.isalnum() else "_" for c in host.upper())


def build_targets(env, roles, models, dev_port=None, transports=None,
                  front_tailnet_port=FRONT_TAILNET_PORT):
    """(targets, unrouted) from env + role map + manifest + per-host transport.

    Each target carries its `transport` and the `addr` that transport says to
    dial: `*_LAN_IP` under `lan`, `*_TAILNET_IP` under `tailnet` (G-MDP4-1 —
    raw IP, no DNS dependency in the inference path). `env_key` names whichever
    key was consulted, so a missing one reports the key you actually need.
    """
    transports = transports or {}
    targets, unrouted = [], []
    for role, hosts in roles.items():
        for host in hosts:
            transport = transports.get(host, "lan")
            port_set, host_unrouted = ports_for_role(
                models, role, dev_port=dev_port, transport=transport,
                front_tailnet_port=front_tailnet_port)
            key = host_key(host) + ("_TAILNET_IP" if transport == "tailnet"
                                    else "_LAN_IP")
            addr = env.get(key)
            for port, service, path in port_set:
                targets.append({
                    "host": host, "role": role, "transport": transport,
                    "addr": addr, "port": port, "service": service,
                    "path": path, "env_key": key,
                })
            for port, service in host_unrouted:
                unrouted.append({"host": host, "role": role,
                                 "transport": transport,
                                 "port": port, "service": service})
    return targets, unrouted


# --------------------------------------------------------------------------- #
# Probing
# --------------------------------------------------------------------------- #
def probe(target, timeout=10.0):
    """GET the target's liveness path. ok=True if the server answered at all."""
    result = dict(target)
    if not target.get("addr"):
        result.update(ok=False, status=None, latency_ms=None,
                      error="no %s in .env" % target["env_key"])
        return result
    url = "http://%s:%s%s" % (target["addr"], target["port"], target["path"])
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
def _render(results, unrouted=()):
    lines = ["%-14s %-8s %-12s %-6s %-9s %-4s %s"
             % ("HOST", "TRANSP", "SERVICE", "PORT", "LATENCY", "EXP", "RESULT")]
    for r in results:
        lat = "---" if r["latency_ms"] is None else "%sms" % r["latency_ms"]
        verdict = "PASS" if r["pass"] else "FAIL"
        detail = "" if r["ok"] and not r["error"] else \
            (" (%s)" % (r["error"] or ("status %s" % r["status"])))
        lines.append("%-14s %-8s %-12s %-6s %-9s %-4s %s%s"
                     % (r["host"], r.get("transport", "lan"), r["service"],
                        r["port"], lat, r["expected"], verdict, detail))
    if unrouted:
        # NOT failures: loopback-bound services on a roaming host that simply
        # have no remote route (N3). Listed so the absence of a row is never
        # mistaken for the probe having forgotten about them.
        lines.append("")
        lines.append("NOT PROBED — no remote route on a `tailnet` host (N3):")
        for u in unrouted:
            lines.append("  ~ %-14s %-12s :%s  (binds 127.0.0.1; `tailscale serve` "
                         "fronts the ollama pipeline port only)"
                         % (u["host"], u["service"], u["port"]))
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--env", default=os.path.join(_REPO, ".env"))
    p.add_argument("--inventory", default=os.path.join(_REPO, "inventory"))
    p.add_argument("--manifest", default=os.path.join(_REPO, "model_manifest.yml"))
    p.add_argument("--host-vars", default=os.path.join(_REPO, "host_vars"),
                   help="dir holding host_vars/<host>.yml (per-host transport)")
    p.add_argument("--only", help="probe only this inventory host")
    p.add_argument("--expect-down", help="this host's probes must FAIL; all others PASS")
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument("--front-tailnet-port", type=int, default=FRONT_TAILNET_PORT,
                   help="port `tailscale serve` publishes on a `tailnet` host")
    args = p.parse_args(argv)

    import plan_model_pulls  # local import; shares scripts/ on sys.path
    env = local_env.load_env(args.env)
    try:
        with open(args.inventory) as fh:
            roles = parse_inventory_roles(fh.read())
    except FileNotFoundError:
        roles = {"small": [], "medium": []}
    models = plan_model_pulls.load_manifest(args.manifest)
    # Second ollama instance ("dev zoo") port, if this deployment has one.
    port_registry = plan_model_pulls.load_ports(args.manifest)

    all_hosts = [h for hosts in roles.values() for h in hosts]
    transports = load_transports(all_hosts, args.host_vars)

    targets, unrouted = build_targets(
        env, roles, models,
        dev_port=port_registry.get("ollama_dev"),
        transports=transports,
        front_tailnet_port=args.front_tailnet_port)
    if args.only:
        targets = [t for t in targets if t["host"] == args.only]
        unrouted = [u for u in unrouted if u["host"] == args.only]

    if not targets:
        sys.stderr.write("no inference backends to probe "
                         "(add hosts to inference_small/inference_medium and .env)\n")
        return 0

    results, ok = run(targets, timeout=args.timeout, expect_down=args.expect_down)
    sys.stdout.write(_render(results, unrouted) + "\n")
    passed = sum(1 for r in results if r["pass"])
    sys.stdout.write("Summary: %d/%d probes as expected. %s\n"
                     % (passed, len(results), "OK" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
