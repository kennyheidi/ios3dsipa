# ios3ds IPA Installer & HFS+ App Manager

An offline `.ipa` sideloader and `/Applications` manager for **[ios3ds](https://github.com/devilapi/ios3ds)** (iOS 4.2.1 running on Nintendo 3DS / ARM11).

Because `ios3ds` runs on an ARM11 (`armv6`) processor and stores its root filesystem inside an HFS+ disk image (`ios3ds/rootfs.hfs`) on the 3DS microSD card, standard iOS sideloaders cannot install apps onto it. `install_ipa-v2.py` provides both a dark-themed **Tkinter GUI** and a **command-line interface** to validate, patch, inject, and manage `.ipa` packages directly inside `rootfs.hfs` from Linux (including immutable distributions like **Bazzite** and **Distrobox** containers).

---

## Features

* **Dual Interface (GUI + CLI):** Launches a dark-themed two-tab desktop GUI when run without arguments (or with `--gui`), or runs headlessly in the terminal for scripting and batch operations.
* **Automatic Path Detection:** Automatically searches Bazzite, Fedora, Ubuntu, and Distrobox mountpoints (`/run/media/$USER`, `/run/host/run/media/$USER`, `/media`, `/mnt`, and `$SD_MOUNT`) for `ios3ds/rootfs.hfs` and `prebuilt/hfsplus-linux-x86_64`.
* **Mach-O Architecture & FairPlay DRM Pre-Flight Check:** Parses Thin (`0xFEEDFACE`) and Universal/Fat (`0xCAFEBABE`) Mach-O headers in memory before installing to verify the binary contains an `armv6` (or `arm_all`) slice and is decrypted (`LC_ENCRYPTION_INFO` `cryptid == 0`).
* **Automatic `Info.plist` OS Version Patching:** Automatically lowers `MinimumOSVersion` values greater than `4.2.1` down to `4.2.1` in `Info.plist` when an `armv6` slice is present.
* **Direct HFS+ Injection & Permission Fixing:** Extracts `Payload/<App>.app`, strips unnecessary `_CodeSignature` metadata, injects the bundle into `/Applications/<App>.app` inside `rootfs.hfs`, and sets `0755` executable permissions on `CFBundleExecutable` and any helper Mach-O binaries or scripts.
* **SpringBoard Cache Clearing & Disk Sync:** Deletes cached SpringBoard icon lists (`com.apple.mobile.installation.plist`, `com.apple.IconsCache`, `com.apple.SpringBoard.folderSwitcherLinen`) so new apps appear immediately on boot, and flushes Linux write caches via `os.sync()`.
* **Installed App Manager:** Lists all `.app` bundles inside `/Applications`, filters out stock iOS 4.2.1 system apps, and cleanly uninstalls sideloaded apps.

---

## Requirements

### Host System (Linux x86_64)
* **Python 3.8+** (uses only Python standard library modules—zero `pip` packages required).
* **`hfsplus-linux-x86_64`** (bundled inside the `ios3ds` release under `prebuilt/hfsplus-linux-x86_64` or built in `build/tools/hfsplus`).
* **`tkinter`** *(Required only for GUI mode)*:
  * **Bazzite / Fedora (inside Distrobox):** `sudo dnf install -y python3-tkinter`
  * **Ubuntu / Debian:** `sudo apt install -y python3-tk`
  * **Arch Linux:** `sudo pacman -S tk`

### Target `.ipa` Packages
1. **ARMv6 CPU Support:** The executable inside the `.ipa` must be compiled for `armv6` (or be a Universal binary containing an `armv6` slice). `armv7`-only binaries will be flagged by the pre-flight checker and will crash with `SIGILL` on the 3DS ARM11 CPU.
2. **Decrypted Binary:** The app must be decrypted (`cryptid == 0`, no FairPlay DRM).

---

## Installation

Place `install_ipa-v2.py` directly inside your extracted `ios3ds` folder (next to `prebuilt/hfsplus-linux-x86_64`) and make it executable:

```bash
cp install_ipa-v2.py ~/3ds-dev/ios3ds-ed83037/
cd ~/3ds-dev/ios3ds-ed83037
chmod +x install_ipa-v2.py prebuilt/hfsplus-linux-x86_64
```

---

## Usage

### 1. Graphical User Interface (GUI Mode)

Launch the GUI by running the script with no arguments (or with `--gui`):

```bash
python3 install_ipa-v2.py
```

You can also pass `.ipa` files directly to pre-populate the GUI install queue on startup:

```bash
python3 install_ipa-v2.py ~/Downloads/RetroGame.ipa ~/Downloads/Utility.ipa
```

**GUI Workflow:**
1. **Verify Paths:** Check Section **1. Target HFS+ Image & Tool Paths** at the top of the window. If your 3DS SD card was mounted after opening the app, click **Auto-Detect** or **SD Folder...** to select your mounted SD card.
2. **Queue `.ipa` Packages:** In the **Install .IPA Packages** tab, click **+ Add .IPA Files...**. Each file is immediately inspected for `armv6` compatibility, FairPlay DRM state, and `MinimumOSVersion`.
3. **Install:** Click **Install Queued IPAs to rootfs.hfs**. Progress is logged in real time in the bottom console pane, followed by an automatic `os.sync()`.
4. **Manage / Uninstall Apps:** Switch to the **Manage Installed Apps (/Applications)** tab to view or remove installed `.app` bundles.

---

### 2. Command-Line Interface (CLI Mode)

Pass your mounted 3DS SD card path (or the direct path to `rootfs.hfs`) followed by one or more `.ipa` files:

```bash
# Install a single .ipa to a mounted SD card
python3 install_ipa-v2.py /run/media/$USER/3DS ~/Downloads/MyGame.ipa

# Install multiple .ipa files at once using $SD_MOUNT
python3 install_ipa-v2.py "$SD_MOUNT" App1.ipa App2.ipa App3.ipa

# Install directly into a local rootfs.hfs file
python3 install_ipa-v2.py ./ios3ds-sd/ios3ds/rootfs.hfs MyGame.ipa
```

**List Installed Applications:**
```bash
python3 install_ipa-v2.py "$SD_MOUNT" --list
```

**Remove an Installed Application:**
```bash
python3 install_ipa-v2.py "$SD_MOUNT" --remove MyGame.app
```

---

## CLI Options Reference

```text
usage: install_ipa-v2.py [-h] [--gui] [--hfsplus HFSPLUS] [--list]
                         [--remove APP_NAME] [--force] [--no-patch-version]
                         [target] [ipas ...]
```

* `target` — Path to your mounted 3DS SD card root (e.g., `/run/media/$USER/3DS`) or direct path to `rootfs.hfs`. Omit to launch the GUI.
* `ipas` — One or more `.ipa` files to validate and install.
* `--gui` — Force launch the Tkinter graphical user interface.
* `--hfsplus PATH` — Explicit path to the `hfsplus-linux-x86_64` binary if not located in `./prebuilt/` or `$PATH`.
* `--list` — List all `.app` bundles currently inside `/Applications` (tagged as `[stock]` or `[user]`) and exit.
* `--remove APP_NAME` — Remove `/Applications/<APP_NAME>` from `rootfs.hfs`, clear SpringBoard caches, sync disk buffers, and exit.
* `--force` — Bypass pre-flight errors and install even if an `armv6` slice is missing or FairPlay encryption (`cryptid != 0`) is detected.
* `--no-patch-version` — Do not automatically downgrade `MinimumOSVersion` values above `4.2.1` in `Info.plist`.

---

## Troubleshooting

* **`ERROR: Python tkinter is not installed in this container/host`**
  Fedora and Bazzite Distrobox containers package `tkinter` separately. Install it inside your container with `sudo dnf install -y python3-tkinter`, or run the script in CLI mode by passing your SD card path and `.ipa` file as arguments.
* **`WARN: No armv6 (armv7)`**
  The `.ipa` was compiled exclusively for `armv7` (iPhone 3GS / iPod touch 3G and newer). Because the Nintendo 3DS uses an `armv6` ARM11 CPU, `armv7
