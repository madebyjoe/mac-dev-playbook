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

### Use with a remote Mac

You can use this playbook to manage other Macs as well; the playbook doesn't even need to be run from a Mac at all! If you want to manage a remote Mac, either another Mac on your network, or a hosted Mac like the ones from [MacStadium](https://www.macstadium.com), you just need to make sure you can connect to it with SSH:

  1. (On the Mac you want to connect to:) Go to System Settings > Sharing.
  2. Enable 'Remote Login'.

> You can also enable remote login on the command line:
>
>     sudo systemsetup -setremotelogin on

Then edit the `inventory` file in this repository. Under the `[headless]` group, comment out the local `mac-headless` line and use the SSH form (there is a commented example in the file):

```
mac-headless ansible_host=[ip or hostname of mac] ansible_user=[mac ssh username] ansible_connection=ssh
```

It makes sense to do this with the `headless` profile as that was what it was designed for.

If you need to supply an SSH password (if you don't use SSH keys), make sure to pass the `--ask-pass` parameter to the `ansible-playbook` command.

### Running a specific set of tagged tasks

You can filter which part of the provisioning process to run by specifying a set of tags using `ansible-playbook`'s `--tags` flag. The tags available are `dotfiles`, `homebrew`, `mas`, `extra-packages`, `config`, `ollama`, and `osx`.

    ansible-playbook main.yml -K --tags "dotfiles,homebrew"

## Post Installation

Some things can be configured automatically from the playbook but a bunch of things require login directly.

1. Get your remote desktop logged into. This playbook uses Parsec
2. Get tailscale authorized by setting up the tailscale client and auth on device.
3. Log into all of the other things that require GUI access