# Shell and terminal environment

The `dotfiles` tag. Covers the prompt, the shell runtime, and Ghostty — the
things that made a fresh work laptop come up with a *working* shell that looked
broken.

```
ansible-playbook main.yml --limit work --tags dotfiles --check --diff   # preview
ansible-playbook main.yml --limit work --tags dotfiles                 # apply
```

Non-GUI hosts skip it entirely: a headless inference box has no terminal to
theme, so the import is guarded to the `personal` and `work` profiles.

## Why this exists

The chain was complete on exactly one machine and reproducible on none. Each
link lived in a different place, and the two that mattered most lived nowhere
durable at all:

| Link | Set by | State before |
|---|---|---|
| `~/.zshrc` → `~/.dotfiles/.zshrc` | dotfiles' `fresh.sh` | symlink, made by hand |
| `ZSH_THEME=powerlevel10k/...` | `.zshrc` | **local only** — `madebyjoe/dotfiles` still ships `agnoster` |
| the theme itself | must be in `$ZSH_CUSTOM/themes/` | **untracked** — in no clone |
| oh-my-zsh | dotfiles' `fresh.sh` | not installed by the playbook |
| `~/.p10k.zsh` (95KB) | `p10k configure` | **one home directory only** — not in the dotfiles repo, and `zsh` is on Mackup's ignore list so iCloud never had it |
| a Nerd Font v3 face | nothing | not installed; only the older Powerline Meslo faces |
| Ghostty config | by hand | default template + theme + one keybind, no `font-family` |

`.zshrc` sets `ZSH_CUSTOM=$DOTFILES`, so oh-my-zsh resolves
`ZSH_THEME=powerlevel10k/powerlevel10k` to
`~/.dotfiles/themes/powerlevel10k` — a path that is untracked upstream. A fresh
clone therefore produced either the old `agnoster` prompt or a reference to a
theme that was not there.

The font is the visible half. `~/.p10k.zsh` is a `POWERLEVEL9K_MODE=nerdfont-v3`
config, so the prompt is drawn out of Nerd Font v3 glyphs. The **"Meslo LG … for
Powerline"** faces already installed — and the ones dotfiles' `core/clone.sh`
fetches from `powerline/fonts` — are the *previous* generation and do not carry
that glyph range. With no v3 font installed and none selected in Ghostty, the
prompt renders as tofu boxes and question marks.

## Division of labour

**The dotfiles repo stays the source of truth for shell content** —
`aliases.zsh`, `path.zsh`, `.zshrc`. This playbook guarantees only what a fresh
clone does not carry: the runtime, the theme checkout, the prompt config, the
font, and the terminal.

That split is why almost every task here is guarded to be non-destructive:

- **Both `git` tasks use `update: false`.** An existing checkout is never
  touched. `~/.dotfiles` routinely holds uncommitted work — a pull or reset here
  would discard it.
- **The clone is HTTPS, not SSH.** This runs during bootstrap, before a key is on
  GitHub.
- **`ZSH_THEME` is corrected with `lineinfile`, not by overwriting `.zshrc`.** A
  clone that is otherwise current keeps everything else it carries.
- **oh-my-zsh installs with `KEEP_ZSHRC=yes`.** Its default is to *move* an
  existing `~/.zshrc` aside and write its own — which here would break the
  symlink and replace a tracked file with a template. `RUNZSH=no` stops it
  exec'ing an interactive zsh and hanging the play.
- **`~/.p10k.zsh` copies with `force: false`.** `p10k configure` rewrites that
  file in place, so a re-tuned local prompt wins over the vendored copy. The task
  exists to stop a fresh machine dropping into the configuration wizard on first
  shell, not to pin the file.
- **The Ghostty config templates with `backup: true`**, because the first run
  replaces a hand-written file.

## Verify

```
ghostty +show-config | grep -E 'font-family|font-size|theme'
ghostty +list-fonts | grep 'MesloLGS Nerd Font Mono'
```

`+list-fonts` is the authority on the family name — `MesloLGS Nerd Font Mono` is
what Ghostty reports for the `font-meslo-lg-nerd-font` cask, and it is **not**
interchangeable with `MesloLGS NF`, the name powerlevel10k's own font installer
uses for its separately-distributed build. If you change
`ghostty_font_family`, confirm the new value against `+list-fonts` first; Ghostty
silently falls back to a default for a family it cannot resolve, which looks
exactly like the bug this tag fixes.

Then, in a **new** Ghostty window (the config is read at startup; `cmd+shift+,`
reloads it):

```
echo $ZSH_THEME        # powerlevel10k/powerlevel10k
ls ~/.dotfiles/themes/powerlevel10k/powerlevel10k.zsh-theme
readlink ~/.zshrc      # /Users/<you>/.dotfiles/.zshrc
```

The prompt should draw its segment separators as solid angled blocks. Question
marks or boxes mean the font did not resolve — recheck `+show-config`.

## Rollback

- **Ghostty** — the previous file is beside it as
  `~/Library/Application Support/com.mitchellh.ghostty/config.<timestamp>~`
  (Ansible's own backup). Restore it and reload with `cmd+shift+,`.
- **`~/.zshrc`** — if the playbook found a real file rather than the symlink, it
  kept it at `~/.zshrc.pre-ansible` before linking. `rm ~/.zshrc && mv
  ~/.zshrc.pre-ansible ~/.zshrc`.
- **Prompt theme** — `git revert` restores the repo, but not machine state:
  `~/.oh-my-zsh`, `~/.dotfiles/themes/powerlevel10k`, and `~/.p10k.zsh` are left
  in place. Remove them by hand if you actually want them gone.
- **Font** — `brew uninstall --cask font-meslo-lg-nerd-font`.

## Known drift

`~/.dotfiles` is **ahead of `github.com/madebyjoe/dotfiles`**. Uncommitted at the
time this tag was written: `.zshrc` (the powerlevel10k rewrite), `core/Brewfile`,
`personal/.mackup.cfg`, `ssh.sh` — plus `themes/powerlevel10k/` untracked.

This tag makes a fresh machine come up correctly *in spite of* that drift rather
than fixing it. The durable fix is to commit and push the dotfiles repo, at which
point the `ZSH_THEME` `lineinfile` task becomes a permanent no-op — harmless, and
worth leaving as a guard.

Two further overlaps worth knowing, neither of which this tag resolves:

- **`core/clone.sh` still installs `powerline/fonts`**, the wrong font
  generation for a `nerdfont-v3` prompt. The Nerd Font now arrives as a Homebrew
  cask instead; the `clone.sh` line is redundant, not harmful.
- **`core/Brewfile` and `group_vars/all.yml` list overlapping casks.** Both are
  idempotent, so running `fresh.sh` and the playbook on one machine is safe — but
  a package added to one list does not appear in the other.
