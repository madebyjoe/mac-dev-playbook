# macOS preference domains (cfprefsd) and Mackup

## The failure mode

macOS reads app preferences through `cfprefsd`, which **will not follow a
symlink**. If `~/Library/Preferences/<domain>.plist` is a symlink, the domain
does not exist as far as the OS is concerned:

```
$ defaults read com.knollsoft.Rectangle
Domain com.knollsoft.Rectangle does not exist
```

The file still holds your settings — `plutil -p` reads it fine — but no app
ever sees them. The app launches, finds nothing, and uses its built-in
defaults. Any change you make is written back through `cfprefsd` and never
reaches the symlinked file, so it is lost on the next launch.

Mackup's default profile symlinks a large set of these into iCloud Drive.
iCloud "Optimize Mac Storage" then evicts them (`ls -lO` shows `dataless`),
which makes recovery worse but is not the root cause — the symlink is.

## Symptoms seen in the wild

| Symptom | Domain |
| --- | --- |
| Rectangle reverts to default hotkeys on every restart | `com.knollsoft.Rectangle` |
| Ghostty quick terminal's ctrl+space works intermittently | `com.apple.symbolichotkeys` |
| Raycast settings not persisting | `com.raycast.macos` |

The Ghostty case is indirect. `com.apple.symbolichotkeys` stores the system
hotkey table. When it is symlinked, macOS falls back to its **defaults**, in
which hotkey **60** ("Select the previous input source") is enabled and bound
to **ctrl+space**. System hotkeys are dispatched ahead of app-registered
global hotkeys, so ctrl+space races between `TextInputSwitcher` and Ghostty —
sometimes nothing happens, sometimes the terminal opens but will not dismiss.

Note this only bites when two or more input sources are registered. A single
U.S. layout plus `com.apple.CharacterPaletteIM` is enough.

## Detection

```sh
# any symlinked preference domain?
find ~/Library/Preferences -maxdepth 1 -type l -name '*.plist'

# is a specific domain actually visible to the OS?
defaults read com.knollsoft.Rectangle >/dev/null 2>&1 \
  && echo OK || echo "MISSING — symlinked or absent"

# is ctrl+space grabbed by the system?
defaults export com.apple.symbolichotkeys - \
  | python3 -c "import sys,plistlib; d=plistlib.load(sys.stdin.buffer); \
print(d['AppleSymbolicHotKeys'].get('60'))"
```

`tasks/configure-macos-prefs.yml` asserts on the first of these, so a plain
`ansible-playbook main.yml --tags macos-prefs` will fail loudly if it returns.

## Recovery

De-symlinking is **not** automated, because a dangling or evicted iCloud
target can mean the only copy of a pref file is remote. Do it by hand:

1. **Back up and materialise.** `cp` forces iCloud to download a `dataless`
   file; if it fails, the target is gone and there is nothing to restore.

   ```sh
   BK=~/pref-backup-$(date +%F); mkdir -p "$BK"
   for l in ~/Library/Preferences/*.plist; do
     [ -L "$l" ] || continue
     cp "$l" "$BK/$(basename "$l")" || echo "DANGLING: $l"
   done
   ```

2. **Quit the owning apps** first, or `cfprefsd` will rewrite state on quit.

3. **Remove the symlink, then `defaults import`.** A plain `cp` back into
   place loses the race with the running cache — `import` is what makes
   `cfprefsd` adopt the file.

   ```sh
   killall cfprefsd
   for f in "$BK"/*.plist; do
     d=$(basename "$f" .plist)
     rm -f ~/Library/Preferences/"$d".plist
     defaults import "$d" "$f"
   done
   ```

4. **Drop dangling symlinks outright** — there is nothing to restore, and
   removing them lets the app write a real plist on next launch.

5. **Verify**: `defaults read <domain>` must print content, not
   "does not exist".

## Gotchas

- `plutil -replace AppleSymbolicHotKeys.60 ...` **does not work** — `plutil`
  reads the numeric key `60` as an array index. Use `plistlib`.
- `defaults write ... -dict-add '{enabled = 0; ...}'` writes the values as
  **strings**. `enabled` must be a real boolean and `parameters` real
  integers, or macOS ignores the entry. Again: use `plistlib`.
- `symbolichotkeys` changes do not reach the running session until
  `activateSettings -u` runs (or you log out).
- Not every symlink is a problem. `~/Library/Application Support/...`
  (Ghostty's config, Cursor's settings) is plain file I/O, not `cfprefsd`, and
  syncs fine — that is how Ghostty's config stays shared across machines.
  Only `~/Library/Preferences` domains are affected.

## Prevention

`mackup_pref_apps_to_ignore` in `group_vars/all.yml` lists every Mackup app
profile that touches `~/Library/Preferences`; the task writes them into
`~/.mackup.cfg` under `[applications_to_ignore]`. Verify with:

```sh
mackup -n -f backup | grep -c 'Library/Preferences'   # must be 0
```
