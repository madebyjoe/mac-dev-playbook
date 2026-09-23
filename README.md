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

### Bootstrapping a brand-new Mac

A factory-fresh Mac has neither Homebrew nor Ansible, and the playbook cannot
install *itself*. These four steps run **by hand, on the target Mac, in this
order**, before the playbook is ever invoked. Each one ends with a `PATH` fix —
that is the part that bites, because both installers put their binaries
somewhere the default `zsh` `PATH` does not look, and both only *print* the fix
rather than applying it.

**1. Apple's command line tools.** Everything else depends on `git` and a C
toolchain:

```
xcode-select --install
```

Wait for the GUI installer to finish before continuing.

**2. Homebrew.** Install it **yourself, before the playbook** — do not rely on
the playbook's fallback task (see *Why Homebrew is a manual step* below):

```
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

It asks for your account password (sudo) and for a RETURN keypress. When it
finishes it prints a "Next steps" block — **those steps are not optional and
are not run for you.** On Apple Silicon, `brew` lands in `/opt/homebrew/bin`,
which is *not* on the default `PATH`:

```
echo >> ~/.zprofile
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
eval "$(/opt/homebrew/bin/brew shellenv)"
```

The last line fixes the *current* shell; the two `echo`s fix every future one.
Confirm with `which brew` → `/opt/homebrew/bin/brew`. (On Intel Macs Homebrew
installs to `/usr/local` and is already on `PATH`; the `shellenv` line is
harmless there.)

**3. Ansible.** Two routes. Now that Homebrew is present, the simplest is:

```
brew install ansible
```

That puts `ansible-playbook` in `/opt/homebrew/bin`, already on `PATH` from
step 2 — nothing further to do.

If you would rather not have Homebrew own Ansible (or you are bootstrapping
before step 2), use the system Python:

```
python3 -m pip install --user ansible
```

**This will appear to do nothing.** `--user` installs the console scripts into a
per-user directory that is not on `PATH`, so `ansible-playbook` comes back
`command not found` even though the install reported success. On a fresh Mac,
where `python3` is Apple's `/usr/bin/python3` (3.9), that directory is
`~/Library/Python/3.9/bin`.

Derive it rather than hardcoding it — and not only because a future macOS will
bump that version. **`python3` may not be the system one.** Once this playbook
has run, pyenv is installed and its shim comes first on `PATH`, and a pyenv or
Homebrew interpreter puts `--user` scripts in `~/.local/bin` instead. Hardcode
the Library path and you will add a directory the install never wrote to, which
looks identical to the failure you are trying to fix. Asking the *same* `python3`
you installed with is right in both cases:

```
PYBIN="$(python3 -c 'import site; print(site.USER_BASE)')/bin"
echo "export PATH=\"$PYBIN:\$PATH\"" >> ~/.zprofile
export PATH="$PYBIN:$PATH"
```

That asks Python where its scripts went instead of trusting `3.9`, but resolves
it **once, now**, and writes the literal path — rather than spawning a Python
interpreter on every shell startup, which you would feel with powerlevel10k's
instant prompt.

Confirm with `which ansible-playbook` and `ansible --version`. If `pip` itself
is missing, `python3 -m ensurepip --user` first.

**4. This repository, and its roles:**

```
git clone <this repo> ~/Code/ansible/mac-dev-playbook
cd ~/Code/ansible/mac-dev-playbook
ansible-galaxy install -r requirements.yml
cp .env.example .env     # see "Local addressing" below; safe to leave empty on a laptop
```

Then run the playbook for the profile you want (see below). Enter your macOS
account password when prompted for the 'BECOME' password.

#### Why Homebrew is a manual step

`main.yml` *does* carry an `Install Homebrew (if missing)` fallback task, and it
runs the same official one-liner. Treat it as a safety net, not the plan: the
Homebrew installer wants a TTY it does not get under Ansible, and it needs sudo
that the play (`become: false`) does not hand it. The task now sets
`NONINTERACTIVE=1` so it can at least complete unattended where sudo is already
cached, but on a genuinely fresh Mac the reliable sequence is step 2 above —
install Homebrew by hand, in a real terminal, and let the playbook find it.

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

**`--limit` selects which host's *variables* apply; the connection decides which *physical machine* runs.** The connection is **derived from `.env`** (`group_vars/all.yml`), so you only ever edit `.env` — never the inventory — to choose local vs. remote:

- **No `MAC_<HOST>_SSH_USER` in `.env`** → `connection=local`: the run happens **on the machine you invoke it from**. This is the default, and the model for running the playbook *on each Mac itself*.
- **`MAC_<HOST>_SSH_USER` set** → `connection=ssh`: that host is **driven remotely** over SSH, to `MAC_<HOST>_SSH_HOST` (or its `MAC_<HOST>_LAN_IP` if unset), as that user.

**Option A — run on the box itself (default).** SSH into the target Mac, make sure it has this repo + its own `.env` (no `SSH_USER` for itself), and run there. `connection=local` binds that machine's own addresses:

```
ssh you@the-mac
cd mac-dev-playbook
ansible-playbook main.yml --limit headless --tags ollama,transcribe --ask-become-pass
```

**Option B — drive a remote Mac from a control node.** No inventory edit:

  1. On the target Mac: System Settings → Sharing → enable **Remote Login** (or `sudo systemsetup -setremotelogin on`), and set up SSH key access.
  2. On the **control node**, add to `.env` (alongside the LAN IP): `MAC_HEADLESS_SSH_USER=youruser` (and optionally `MAC_HEADLESS_SSH_HOST=` a hostname/tailnet name; it defaults to the LAN IP).
  3. Run from the control node: `ansible-playbook main.yml --limit headless --tags ollama,transcribe --ask-become-pass`.

Put the `SSH_USER` key **only in the control node's `.env`**, not in the box's own `.env` (or it would try to SSH to itself). Under Option B the work splits cleanly: the **`.env` is read on the control node** (addresses become facts on the target); the planner + manifest are **staged to the target** so they run against its own disk/`ollama list`; models, services, and plists are created **on the target**; and the emitted LiteLLM snippet lands in **`artifacts/` on the control node**. `--ask-become-pass` supplies the *remote* sudo password (for `pmset`); add `--ask-pass` if you use an SSH password instead of a key.

### Running a specific set of tagged tasks

`--tags` filters which part of the provisioning run executes. Tags are a **union**,
not an intersection: `--tags ollama,mas` runs everything tagged *either*.

| Tag | Runs | Applies to |
|---|---|---|
| `homebrew` | taps, CLI packages, casks (+ the strict failure gate) | every profile |
| `mas` | Mac App Store apps (+ the strict failure gate) | every profile |
| `herdr` | the herdr vendor-script install | every profile, **work included** |
| `dotfiles` | shell + terminal: oh-my-zsh, powerlevel10k, `~/.zshrc` link, Ghostty config | `personal`, `work` |
| `config` | all three service-config tasks: ollama, llama.cpp, transcribe | non-work |
| `ollama` | ollama service config **+** model plan/pull **+** residency check **+** router snippet | non-work |
| `llamacpp` | the per-model llama.cpp launchd services | inference-role hosts |
| `transcribe` | the whisper-server service on the M1 Pro | `inference_small` only |
| `models` | the storage-aware planner/pulls + the residency check | inference-role hosts |
| `litellm` | re-emit `artifacts/litellm/<host>.yml` | inference-role hosts |
| `verify` | the `/api/ps` residency assertion, on its own | inference-role hosts |
| `power` | `pmset -c sleep 0` (needs sudo) | inference-role hosts |

Anything not in an inference-role group skips the inference tags entirely, so
`--tags ollama` on a work Mac is a no-op rather than an error.

#### Common runs

**Set up or top up a normal machine.** The everyday run — safe to repeat, and the
default for a laptop:

```
ansible-playbook main.yml --limit personal --ask-become-pass
ansible-playbook main.yml --limit work --ask-become-pass
```

**Just installed something new in a package list?** Skip straight to it:

```
ansible-playbook main.yml --limit personal --tags homebrew
ansible-playbook main.yml --limit personal --tags homebrew,mas   # both install layers
ansible-playbook main.yml --limit work --tags herdr              # just herdr
```

**Preview before committing.** `--check` renders every template and shows the
model plan without changing anything. Pair it with `--diff` to see the actual
plist changes:

```
ansible-playbook main.yml --limit headless --check --diff --ask-become-pass
```

**Bring up the M1 Pro as an inference node.** Two passes on purpose — look at the
plan first, then let it pull:

```
# 1. dry: renders plists, prints the model plan, pulls nothing
ansible-playbook main.yml --limit headless --tags ollama,transcribe --ask-become-pass

# 2. for real: downloads models, starts services, asserts residency
ansible-playbook main.yml --limit headless --tags ollama,transcribe \
  -e model_pull_dry_run=false --ask-become-pass
```

**Touch the services but not the models.** Re-render and bounce the launchd
services only — no planner, no pulls, no snippet:

```
ansible-playbook main.yml --limit headless --tags config --ask-become-pass
```

**Narrower still**, when you only care about one service:

```
ansible-playbook main.yml --limit headless --tags transcribe            # whisper-server only
ansible-playbook main.yml --limit headless --tags llamacpp              # llama.cpp only
ansible-playbook main.yml --limit headless --tags ollama --skip-tags models,litellm
```

**Is the residency invariant still holding?** A read-only check against
`/api/ps` — no pulls, no service changes. This is the one to run days later, or
after someone has been experimenting on the box:

```
ansible-playbook main.yml --limit headless --tags verify
```

**Re-emit the router snippet** after editing the manifest, without touching the
machine (the snippet lands in `artifacts/litellm/` on the control node):

```
ansible-playbook main.yml --limit headless --tags litellm
```

**Stop an inference node from sleeping**, on its own:

```
ansible-playbook main.yml --limit headless --tags power --ask-become-pass
```

**Drive a remote Mac** — identical commands; only `.env` decides local vs. SSH
(see *Which machine does a run target?* above):

```
ansible-playbook main.yml --limit headless --tags ollama --ask-become-pass
```

#### Flags worth knowing

| Flag | Effect |
|---|---|
| `--limit <profile>` | **Mandatory.** `personal`, `work`, or `headless`. |
| `--ask-become-pass` / `-K` | Needed whenever `power`/`pmset` or the Rosetta install runs. |
| `--check` / `--diff` | Preview. Templates render, plans print, nothing is installed or started. |
| `-e model_pull_dry_run=false` | **Actually download models.** Default is a dry plan — nothing is pulled without this. |
| `-e soft_fail=true` | Downgrade the strict install gate to a summary instead of failing the run. |
| `-e allow_loopback=true` | Let an inference-role host run without a LAN IP in `.env` (laptop dev). |
| `-e skip_residency_check=true` | Skip the `/api/ps` assertion. |
| `-e herdr_enabled=false` | Skip herdr on this run. |
| `-e herdr_force_install=true` | Re-run the herdr installer even though it is already on `PATH` (upgrade). |

Two defaults are deliberate and worth repeating: **no model is ever downloaded
without `-e model_pull_dry_run=false`**, and **a failed tap/package/cask/MAS app
fails the whole run** unless you pass `-e soft_fail=true`.

### Shell and terminal (`dotfiles`)

A fresh laptop came up with a *working* shell that looked broken: no prompt
theme, tofu glyphs instead of icons, stock Ghostty. Nothing was misconfigured —
the setup simply was not reproducible. Every link in the chain lived in a
different place, and the two that mattered most lived nowhere durable:
`ZSH_THEME=powerlevel10k/powerlevel10k` was set only in an **uncommitted**
`.zshrc`, the theme itself was **untracked** (so absent from any clone), and the
95KB `~/.p10k.zsh` existed in exactly one home directory — not in the dotfiles
repo, and `zsh` is on Mackup's ignore list, so iCloud never had it either.

The `dotfiles` tag closes that. It installs oh-my-zsh, checks out powerlevel10k
where `ZSH_CUSTOM` expects it, links `~/.zshrc` to the dotfiles copy, installs
the vendored prompt config, and renders the Ghostty config:

```
ansible-playbook main.yml --limit work --tags dotfiles --check --diff   # preview
ansible-playbook main.yml --limit work --tags dotfiles                 # apply
```

**The division of labour matters.** The dotfiles repo
(`github.com/madebyjoe/dotfiles`) remains the source of truth for shell
*content* — `aliases.zsh`, `path.zsh`, `.zshrc`. This playbook guarantees only
what a fresh clone does not carry. So every task here is deliberately
non-destructive: both `git` tasks use `update: false` (never touch an existing
checkout — `~/.dotfiles` routinely holds uncommitted work), the clone is HTTPS
rather than SSH (this runs before a key is on GitHub), `ZSH_THEME` is corrected
with `lineinfile` rather than by overwriting `.zshrc`, `~/.p10k.zsh` copies with
`force: false` so a locally re-tuned prompt wins, and the Ghostty template takes
a backup because its first run replaces a hand-written file.

**The font is the visible half.** `~/.p10k.zsh` is a `nerdfont-v3` config, so the
prompt is drawn from Nerd Font v3 glyphs. The "Meslo LG … for Powerline" faces
that were installed — and the ones dotfiles' `core/clone.sh` fetches from
`powerline/fonts` — are the *previous* generation and lack that glyph range.
`font-meslo-lg-nerd-font` is now a core cask and Ghostty selects
`MesloLGS Nerd Font Mono`, the exact family `ghostty +list-fonts` reports for it.
Change `ghostty_font_family` only against that command's output: Ghostty falls
back silently for a family it cannot resolve, which looks precisely like the bug
this tag fixes.

Ghostty's config is written to the **Application Support** path, not
`~/.config/ghostty` — that is the one `ghostty +show-config` proves is live on
macOS, and the one the hand-written config already occupied. Keeping to a single
file stops two configs fighting over the same key. The rendered result reproduces
the previous theme and the `ctrl+space` quick-terminal keybind, so the only
intended change is a font that resolves.

Headless hosts skip the tag entirely — an inference box has no terminal to theme.
`runbooks/shell.md` has the verification commands, the rollback paths, and the
outstanding dotfiles-repo drift.

### Non-Homebrew installs

Most software comes from Homebrew or the App Store. **herdr** does not — it ships a
vendor install script (`curl -fsSL https://herdr.dev/install.sh | sh`), so it lives
in `tasks/install-herdr.yml` rather than a package list. It is installed on **every
profile, work machines included**, and only when `herdr` is not already on `PATH`
(`~/.local/bin` and `~/bin` are probed too), so re-runs do not silently re-fetch and
re-execute a remote script.

Two things worth knowing: the install is **not version-pinned** — whatever the
vendor serves at run time is what lands (`herdr_install_url` is a variable, so a
pinned or mirrored URL can be substituted) — and it is skippable per run.

```
ansible-playbook main.yml --limit work --tags herdr      # just herdr
ansible-playbook main.yml --limit work -e herdr_enabled=false     # skip it
ansible-playbook main.yml --limit work -e herdr_force_install=true  # reinstall/upgrade
```

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

#### Model residency: two ollama instances on the small node

An `inference_small` host runs **two** ollama servers. `com.ollama.serve` on
`:11434` holds only the alias-serving models (`tier: pipeline` in the manifest —
`small` and `embed`); `com.ollama.dev` on `:11435` holds the ad-hoc `dev/*` zoo
with its own slot budget and a *finite* keep-alive. They share one model store, so
nothing is downloaded twice — only residency is partitioned.

This exists because the zoo sharing one instance was evicting the pipeline models:
`small` was taking 16–76s for a prompt the M4 Pro answered in 2.5s, and `embed`
intermittently returned `Compute error.` 500s. `OLLAMA_KEEP_ALIVE=-1` does **not**
prevent that — at the slot cap ollama evicts the least-recently-used model
regardless of keep-alive. `runbooks/ollama.md` has the full diagnosis, the slot
arithmetic, and the residency invariant the run now asserts against `/api/ps`
rather than trusting.

### Headless inference quickstart

The exact sequence to bring a headless Mac up as an inference backend, end to end (the recipes above are the à-la-carte version). A bare `--limit headless` run **without** role-group membership intentionally yields a loopback-only node (F4) — role membership is what turns on LAN serving.

1. **Add the host to a role group** in `inventory` (membership only): put `mac-headless` under `[inference_small]` (M1 Pro) or `[inference_medium]` (M4 Pro).
2. **Fill `.env`** with its LAN IP (`MAC_HEADLESS_LAN_IP=...`, matching the router's static DHCP reservation; G30 = LAN). See `.env.example`.
3. **Dry check** — review the model plan and the rendered plist/snippet diffs before anything happens. `-K` is needed for the `pmset` never-sleep task:

   ```
   ansible-playbook main.yml --limit headless --check --diff --ask-become-pass
   ```

4. **Provision for real** — pulls models, starts services, warms the pinned models and asserts residency:

   ```
   ansible-playbook main.yml --limit headless -e model_pull_dry_run=false --ask-become-pass
   ```

5. **Verify the backend:** `python3 scripts/probe_backends.py` (or `--only mac-headless`). Note a role node binds `<lan_ip>:11434` **only** — not `127.0.0.1` — so on the box itself the `ollama` CLI/app need `export OLLAMA_HOST=<lan_ip>:11434` (see `runbooks/ollama.md`). The provisioning run prints where it is serving. On an `inference_small` host the dev zoo is a *second* server on `:11435`.
6. **Reconcile the router** (human, on Unraid): diff the emitted `artifacts/litellm/mac-headless.yml` against the router's `config.yaml`, fix `api_base` values (see `runbooks/verification.md`), then `docker compose restart litellm`. **Every `dev/*` entry must point at `:11435`** — pointing one back at `:11434` re-creates the model-slot thrashing the split exists to prevent.
7. **Alias acceptance** (human): run the router-side `smoke-test.sh` with the `vk-smoke` key — **at least three times**. The `embed` failure it guards against was intermittent at 2-in-3, so one green run proves nothing.
8. **Later, and this is the real gate:** re-check residency after the box has been idle for hours, or after anyone has been experimenting on it:

   ```
   ansible-playbook main.yml --limit headless --tags verify
   ```

   A fix that only holds while you are watching it is not a fix.

## Runbooks

- [runbooks/shell.md](runbooks/shell.md) — the shell + terminal environment: why a fresh laptop's prompt came up broken, what the `dotfiles` tag owns vs. what the dotfiles repo owns, verification and rollback.
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