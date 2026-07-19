# Mac Development Ansible Playbook

This playbook installs and configures most of the software I use on my Mac for web and software development. Some things in macOS are slightly difficult to automate, so I still have a few manual installation steps, but at least it's all documented here.

## Good Prerequisites

Some things are not possible to bootstrap remotely on mac os and as such these are the minimum steps that will make it easier to automate the rest of the playbook.

- Sign into Apple Account. That means iCloud, AppStore at least. 
- Follow the instructions below for remote install by turning on "Remote Login".
- Copy your current ssh key to the other device with `ssh-copy-id user@host`
- Change the battery settings to never sleep if this is going to be a headless laptop install.
- If you can get one of those HDMI dummy plugs, then you don't have to worry about virtual displays and other quirks when using remote desktop. It's worth it.

## Installation

  1. Ensure Apple's command line tools are installed (`xcode-select --install` to launch the installer).
  2. [Install Ansible](https://docs.ansible.com/ansible/latest/installation_guide/index.html):
  3. Clone or download this repository to your local drive.
  4. Run `ansible-galaxy install -r requirements.yml` inside this directory to install required Ansible roles.
  5. Run the playbook for the profile you want (see below). Enter your macOS account password when prompted for the 'BECOME' password.

> **`--limit` is mandatory.** Each profile (`personal`, `work`, `headless`) is a distinct host alias that all resolve to this machine. Ansible resolves variables per host, so without `--limit` every profile's variables merge together and profile-specific packages are silently dropped. Always pass exactly one profile:
>
> ```
> ansible-playbook main.yml --limit personal
> ansible-playbook main.yml --limit work
> ansible-playbook main.yml --limit headless
> ```
>
> Because all three aliases point at the same machine, `ansible all -m ping` reports three hosts on one Mac. This is cosmetic and expected.

> Note: If some Homebrew commands fail, you might need to agree to Xcode's license or fix some other Brew issue. Run `brew doctor` to see if this is the case.

### Which machine does a run target?

**`--limit` selects which host's *variables* apply; the host's `ansible_connection` decides which *physical machine* runs.** By default every profile is `ansible_connection=local`, so `--limit headless` **runs on the machine you invoke it from** — not on some other box named "headless". This repo is designed to run **locally on each target Mac**. There are two ways to provision a Mac:

**Option A — run on the box itself (default).** SSH into the target Mac, make sure it has this repo checked out with its own `.env` filled in, and run there:

```
ssh you@the-mac
cd mac-dev-playbook
ansible-playbook main.yml --limit headless --tags ollama,transcribe --ask-become-pass
```

Because you're on the box, `connection=local` is correct and it binds that machine's own addresses.

**Option B — drive a remote Mac from a control node (over SSH).** You can run the playbook from one machine (or a non-Mac control node) against another Mac:

  1. On the target Mac: System Settings → Sharing → enable **Remote Login** (or `sudo systemsetup -setremotelogin on`), and set up SSH key access.
  2. In `inventory`, switch that host to the SSH form:
     ```
     [headless]
     mac-headless ansible_host=[ip or hostname] ansible_user=[ssh user] ansible_connection=ssh
     ```
  3. Run from the control node, e.g. `ansible-playbook main.yml --limit headless --tags ollama,transcribe --ask-become-pass`.

Under Option B the split of work is: the **`.env` is read on the control node** (its addresses are pushed to the target as facts); the planner + manifest are **staged to the target** so they run against the target's own disk/`ollama list`; models, services, and plists are created **on the target**; and the emitted LiteLLM snippet lands in **`artifacts/` on the control node** for you to apply on Unraid. `--ask-become-pass` supplies the *remote* sudo password (for the `pmset` task); add `--ask-pass` if you use an SSH password instead of a key.

### Running a specific set of tagged tasks

You can filter which part of the provisioning process to run by specifying a set of tags using `ansible-playbook`'s `--tags` flag. The tags available are `homebrew`, `mas`, `config`, `ollama`, `llamacpp`, `models`, `litellm`, and `power`.

    ansible-playbook main.yml --limit personal --tags "homebrew"

### Local addressing (`.env`)

Host addresses are **not** stored in this repo (it is public; the durable fix is G27, Gitea origin). They live in a gitignored `.env` at the repo root. Copy the template, fill it in, and never commit it:

```
cp .env.example .env
# edit .env: each host's LAN IP (and, for management only, its Tailscale IP/name)
```

Keys are per-host, hostname `UPPER_SNAKE_CASE`-prefixed — `MAC_HEADLESS_LAN_IP`, `MAC_HEADLESS_TAILNET_IP`, `MAC_HEADLESS_TAILNET_NAME`, etc. Under **G30 = LAN**, inference binds and the LiteLLM `api_base` use `*_LAN_IP` (set static DHCP reservations that match the router's `M1PRO_IP`/`M4PRO_IP`); Tailscale stays for SSH/management only. The playbook loads `.env` in `pre_tasks` and `set_fact`s `lan_ip`/`tailnet_ip`/`tailnet_name` per host — these **override** the group_vars defaults.

`.env` holds addresses only — **no credentials** (F10). Back it up (1Password secure note or the private Gitea mirror); it is also on the sanitization deny-list. `.env.example` documents every key with placeholders.

### Ollama network binding

The Ollama launchd service binds to **loopback (`127.0.0.1:11434`) by default** — Ollama has no auth, so a non-loopback bind would expose an unauthenticated API. A host in an `inference_small`/`inference_medium` group instead binds its **LAN IP** (`ollama_bind`, from `lan_ip`), keeps the model resident, drives storage-aware model pulls, and emits a LiteLLM router snippet into `artifacts/litellm/`.

- `ollama_bind` — the `OLLAMA_HOST` value written into the plist, `<lan_ip>:11434`, set by the inference-role group_vars from the host's `.env` `*_LAN_IP`.
- A role-group host with **no** LAN IP in `.env` **fails the run** naming the missing key (F15); pass `-e allow_loopback=true` for laptop dev without a filled `.env`.

Work-profile machines never get Ollama configured at all (see the work guard in `main.yml`). See the runbooks below.

### Headless inference quickstart

The exact sequence to bring a headless Mac up as an inference backend. A bare `--limit headless` run **without** role-group membership intentionally yields a loopback-only node (F4) — role membership is what turns on LAN serving.

1. **Add the host to a role group** in `inventory` (membership only): put `mac-headless` under `[inference_small]` (M1 Pro) or `[inference_medium]` (M4 Pro).
2. **Fill `.env`** with its LAN IP (`MAC_HEADLESS_LAN_IP=...`, matching the router's static DHCP reservation; G30 = LAN). See `.env.example`.
3. **Dry check:** `ansible-playbook main.yml --limit headless --ask-become-pass --check` (`--ask-become-pass`/`-K` is needed for the `pmset` never-sleep task). Review the plan and the rendered plist/snippet diffs.
4. **Provision for real** (pulls models, starts services): rerun without `--check`, adding `-e model_pull_dry_run=false`.
5. **Verify the backend:** `python3 scripts/probe_backends.py` (or `--only mac-headless`).
6. **Reconcile the router** (human, on Unraid): diff the emitted `artifacts/litellm/mac-headless.yml` against the router's `config.yaml`, fix `api_base` values (see `runbooks/verification.md`), then `docker compose restart litellm`.
7. **Alias acceptance** (human): run the router-side `smoke-test.sh` with the `vk-smoke` key.

## Runbooks

- [runbooks/ollama.md](runbooks/ollama.md) — the Ollama launchd service: where it binds and why, restart/stop, logs, rollback.
- [runbooks/model-management.md](runbooks/model-management.md) — the model manifest schema, running the storage-aware planner, and how eviction recommendations are handled (by you).
- [runbooks/llamacpp.md](runbooks/llamacpp.md) — the llama.cpp (second engine) per-model launchd services: start/stop, logs, swapping a quant.
- [runbooks/transcribe.md](runbooks/transcribe.md) — the `transcribe` (whisper-server, `:8082`) service; `embed` served by ollama `:11434`.
- [runbooks/verification.md](runbooks/verification.md) — the two-layer test model: `probe_backends.py` (backend liveness) + the router-side `smoke-test.sh` (alias end-to-end).
- [docs/rollback-notes.md](docs/rollback-notes.md) — per-task rollback procedures.
- [docs/decision-gates.md](docs/decision-gates.md) — engine policy (F6.1) and the status of decision gates G25–G29.

## Post Installation

Some things can be configured automatically from the playbook but a bunch of things require login directly.

1. Get your remote desktop logged into. This playbook uses Parsec
2. Get tailscale authorized by setting up the tailscale client and auth on device.
3. Log into all of the other things that require GUI access