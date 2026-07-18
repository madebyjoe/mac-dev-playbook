#!/usr/bin/env python3
"""Parse the repo-root .env into a dict (stdlib only, MDP-2 F10).

The .env holds per-host ADDRESSES only (never credentials): hostname-prefixed
keys like MAC_HEADLESS_LAN_IP / _TAILNET_IP / _TAILNET_NAME. It is gitignored;
`.env.example` documents every key. Format is plain KEY=VALUE with `#` comments
and blank lines — no quoting tricks. This module is the single parser shared by
the Ansible loader (`tasks/load-local-env.yml`, via --json) and the backend
probe (`scripts/probe_backends.py`).
"""

import argparse
import json
import sys


def parse_env(text):
    """Parse KEY=VALUE lines into a dict; ignore blanks and `#` comment lines."""
    out = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, val = stripped.split("=", 1)  # split on the FIRST '=' only
        key = key.strip()
        if key:
            out[key] = val.strip()
    return out


def load_env(path):
    """Read and parse an .env file; return {} if it does not exist."""
    try:
        with open(path) as fh:
            return parse_env(fh.read())
    except FileNotFoundError:
        return {}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("path", help="path to the .env file")
    p.add_argument("--json", action="store_true",
                   help="emit the parsed env as a JSON object (for Ansible)")
    args = p.parse_args(argv)
    env = load_env(args.path)
    if args.json:
        sys.stdout.write(json.dumps(env))
    else:
        for k, v in env.items():
            sys.stdout.write("%s=%s\n" % (k, v))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
