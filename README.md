# ios3ds IPA Installer & HFS+ App Manager (`install_ipa-v4.py`)

An offline `.ipa` sideloader and `/Applications` manager for **[ios3ds](https://github.com/devilapi/ios3ds)** (iOS 4.2.1 running on Nintendo 3DS / ARM11).

Because `ios3ds` runs on an ARM11 (`armv6`) processor and stores its root filesystem inside an HFS+ disk image (`ios3ds/rootfs.hfs`) on a FAT32 3DS microSD card, standard iOS sideloaders cannot install apps onto it. `install_ipa-v4.py` provides both a dark-themed **Tkinter GUI** and a **command-line interface** to validate, patch, auto-expand `rootfs.hfs` up to **3.0 GiB**, inject `.ipa` packages with safe fallback handling, and manage installed apps directly from Linux (including immutable distributions like **Bazzite** and **Distrobox** containers).

---

## Features

* **Automatic 3.0 GiB `rootfs.hfs` Expansion & Bitmap Recovery:** By default, `ios3ds` builds a 1.0 GiB `rootfs.hfs` image with ~120 MB of free space. `install_ipa-v4.py` automatically expands `rootfs.hfs` to **3.0 GiB (`3,221,225,472` bytes)**—providing over 2.1 GiB of free space for games while staying safely below FAT32's 4 GiB single-file limit. Also includes a **Grow to 3.0 GiB Now** button and automatic block-reclamation if a previous failed install filled the 1.0 GiB image to 0 free blocks.
* **Safe File-by-File HFS+ Injection Fallback:** Strips broken symlinks inside `.app` bundles before injection and automatically falls back from bulk `hfsplus addall` to explicit per-directory `mkdir` and per-file `add` operations if `xpwn`'s `addall` hits its internal 1024-byte path buffer or `chdir()` bug.
* **Dual Interface (GUI + CLI):** Launches a dark-themed two-tab desktop GUI when run without arguments (or with `--gui`), or runs headlessly in the terminal for scripting and batch operations.
* **Automatic Path Detection:** Searches Bazzite, Fedora, Ubuntu, and Distrobox mountpoints (`/run/media/$USER`, `/run/host/run/media/$USER`, `/media`, `/mnt`, and `$SD_MOUNT`) for `ios3ds/rootfs.hfs` and `prebuilt/hfsplus-linux-x86_64`.
* **Mach-O Architecture & FairPlay DRM Pre-Flight Check:** Parses Thin (`0xFEEDFACE`) and Universal/Fat (`0xCAFEBABE`) Mach-O headers in memory before installing to verify the binary contains an `armv6` (or `arm_all`) slice and is decrypted (`LC_ENCRYPTION_INFO` `cryptid == 0`).
* **Automatic `Info.plist` OS Version Patching (with Binary Plist Fallback):** Automatically lowers `MinimumOSVersion` values greater than `4.2.1` down to `4.2.1` in `Info.plist`, and includes a fallback parser for older iOS 2.x/3.x binary plists with non-UTF-8 strings.
* **SpringBoard Cache Clearing & Disk Sync:** Strips `_CodeSignature`, `SC_Info`, and `iTunesMetadata.plist`, sets `0755` permissions on all Mach-O binaries and scripts inside the bundle, clears cached SpringBoard icon state (`com.apple.mobile.installation.plist`, `com.apple.IconsCache`, `com.apple.SpringBoard.folderSwitcherLinen`), and flushes Linux write buffers via `os.sync()`.
* **Space-Safe Installed App Manager:** Lists all `.app` bundles inside `/Applications` (including app bundles with spaces in their filenames like `Sonic 1.app` and `Sonic 2.app`), filters out stock iOS 4.2.1 system apps, and cleanly uninstalls sideloaded apps.

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

Place `install_ipa-v4.py` directly inside your extracted `ios3ds` folder (next to `prebuilt/hfsplus-linux-x86_64`) and make it executable:

```bash
cp install_ipa-v4.py ~/3ds-dev/ios3ds-ed83037/
cd ~/3ds-dev/ios3ds-ed83037
chmod +x install_ipa-v4.py prebuilt/hfsplus-linux-x86_64
```

---

## Usage

### 1. Graphical User Interface (GUI Mode)

Launch the GUI by running the script with no arguments (or with `--gui`):

```bash
python3 install_ipa-v4.py
```

You can also pass `.ipa` files directly to pre-populate the GUI install queue on startup:

```bash
python3 install_ipa-v4.py ~/Downloads/Sonic_1_v1.1.3_os221.ipa ~/Downloads/Angry_Birds_v1.4.1_os30.ipa
```

**GUI Workflow:**
1. **Verify Paths & RootFS Size:** Check Section **1. Target HFS+ Image & Tool Paths** at the top of the window. The **Size: X.XX / 3.00 GiB** badge displays the current size of your `rootfs.hfs` file. You can click **Grow to 3.0 GiB Now** at any time, or leave **Auto-grow rootfs.hfs to 3.0 GiB before install** checked.
2. **Queue `.ipa` Packages:** In the **Install .IPA Packages** tab, click **+ Add .IPA Files...**. Each file is immediately inspected for `armv6` compatibility, FairPlay DRM state, and `MinimumOSVersion`.
3. **Install:** Click **Install Queued IPAs to rootfs.hfs**. Progress is logged in real time in the bottom console pane, followed by an automatic `os.sync()`.
4. **Manage / Uninstall Apps:** Switch to the **Manage Installed Apps (/Applications)** tab to view or remove installed `.app` bundles.

---

### 2. Command-Line Interface (CLI Mode)

Pass your mounted 3DS SD card path (or the direct path to `rootfs.hfs`) followed by one or more `.ipa` files:

```bash
# Install a single .ipa to a mounted SD card (auto-grows rootfs.hfs to 3.0 GiB)
python3 install_ipa-v4.py /run/media/$USER/3DS ~/Downloads/MyGame.ipa

# Install multiple .ipa files at once using $SD_MOUNT
python3 install_ipa-v4.py "$SD_MOUNT" App1.ipa App2.ipa App3.ipa

# Install with a custom rootfs size limit (e.g., 2.5 GiB)
python3 install_ipa-v4.py --rootfs-size-gb 2.5 "$SD_MOUNT" MyGame.ipa
```

**List Installed Applications:**
```bash
python3 install_ipa-v4.py "$SD_MOUNT" --list
```

**Remove an Installed Application:**
```bash
python3 install_ipa-v4.py "$SD_MOUNT" --remove "Sonic 2.app"
```

---

## CLI Options Reference

```text
usage: install_ipa-v4.py [-h] [--gui] [--hfsplus HFSPLUS]
                         [--rootfs-size-gb ROOTFS_SIZE_GB] [--list]
                         [--remove APP_NAME] [--force] [--no-patch-version]
                         [target] [ipas ...]
```

* `target` — Path to your mounted 3DS SD card root (e.g., `/run/media/$USER/3DS`) or direct path to `rootfs.hfs`. Omit to launch the GUI.
* `ipas` — One or more `.ipa` files to validate and install.
* `--gui` — Force launch the Tkinter graphical user interface.
* `--hfsplus PATH` — Explicit path to the `hfsplus-linux-x86_64` binary if not located in `./prebuilt/` or `$PATH`.
* `--rootfs-size-gb FLOAT` — Target `rootfs.hfs` size in GiB before installing (default: `3.0`, max ~`3.9` on FAT32).
* `--list` — List all `.app` bundles currently inside `/Applications` (tagged as `[stock]` or `[user]`) and exit.
* `--remove APP_NAME` — Remove `/Applications/<APP_NAME>` from `rootfs.hfs`, clear SpringBoard caches, sync disk buffers, and exit.
* `--force` — Bypass pre-flight errors and install even if an `armv6` slice is missing or FairPlay encryption (`cryptid != 0`) is detected.
* `--no-patch-version` — Do not automatically downgrade `MinimumOSVersion` values above `4.2.1` in `Info.plist`.

---

## Potential Errors & Troubleshooting

### 1. `FAILED: hfsplus addall ...` / `ASSERT(allocate(rawFile, location + size), "allocate")`
* **Cause:** Either your `rootfs.hfs` is still at its stock 1.0 GiB size (~120 MB free) and ran out of free HFS+ blocks mid-install, or `xpwn`'s `addall` C function choked on a symlink or deep subfolder tree inside the `.ipa`.
* **How `install_ipa-v4.py` fixes it:** The script automatically grows `rootfs.hfs` to 3.0 GiB before installing, strips symlinks inside extracted `.app` bundles, and automatically switches to a safe file-by-file `mkdir` + `add` fallback if bulk `addall` returns a non-zero exit code.

### 2. `Not enough room to allocate new allocation map blocks` during Grow
* **Cause:** In `xpwn-hfsplus` (`src/xpwn-hfsplus/hfs/hfslib.c`), `grow_hfs()` requires ~16 free HFS+ blocks (64 KB) *already inside* the existing 1.0 GiB volume to write the expanded HFS+ allocation bitmap for a 3.0 GiB volume. If an earlier failed installation filled the 1.0 GiB image to literally **0 free blocks**, `hfsplus grow` cannot allocate the new bitmap.
* **How `install_ipa-v4.py` fixes it:** When `ensure_rootfs_size()` detects this message, it automatically runs `reclaim_blocks_for_bitmap()` to remove half-installed non-stock `.app` folders and expendable cache files from `rootfs.hfs`, step-grows the volume by +64 MiB, and then expands it to the full 3.0 GiB.
* **Manual Terminal Fix (if needed):**
  ```bash
  ./prebuilt/hfsplus-linux-x86_64 /path/to/rootfs.hfs rmall "/Applications/PartialApp.app"
  ./prebuilt/hfsplus-linux-x86_64 /path/to/rootfs.hfs grow 3221225472
  ```
* **Permanent Build Fix:** To make `ios3ds` generate a 3.0 GiB image from the very beginning when building from an IPSW, edit `rootfs/recipe.json` and change `"grow_bytes": 1073741824` to `"grow_bytes": 3221225472`.

### 3. `WARN: No armv6 (armv7)`
* **Cause:** The `.ipa` was compiled exclusively for `armv7` (iPhone 3GS / iPod touch 3G and newer). Because the Nintendo 3DS uses an `armv6` ARM11 CPU, `armv7` instructions will trigger an immediate crash (`SIGILL` / `Illegal instruction: 4`).
* **Fix:** Use an earlier version of the `.ipa` targeting iOS 2.x–4.2.1 that includes an `armv6` slice.

### 4. `WARN: FairPlay Encrypted`
* **Cause:** The executable inside the `.ipa` still has Apple FairPlay DRM enabled (`LC_ENCRYPTION_INFO` `cryptid != 0`).
* **Fix:** `ios3ds` cannot decrypt FairPlay binaries at runtime; use a decrypted `.ipa` package.

### 5. `ERROR: Python tkinter is not installed in this container/host`
* **Cause:** Fedora and Bazzite Distrobox containers package `tkinter` separately from base Python.
* **Fix:** Run `sudo dnf install -y python3-tkinter` inside your Distrobox container, or use the script via its CLI mode.

### 6. FAT32 4 GiB File Size Limit
* **Cause:** The Nintendo 3DS requires a FAT32-formatted microSD card, and FAT32 has a hard maximum single-file size of `4,294,967,295` bytes (4 GiB minus 1 byte).
* **Fix:** Keep `--rootfs-size-gb` at `3.0` (the default) or no higher than `3.8` so `ios3ds/rootfs.hfs` never hits the FAT32 file size ceiling.
