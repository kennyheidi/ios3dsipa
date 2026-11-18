#!/usr/bin/env python3
"""
install_ipa_gui.py - Offline .ipa Installer & App Manager for ios3ds (GUI + CLI)

Features:
- Modern dark-themed Tkinter GUI (automatically launches when run with no CLI args or --gui)
- Auto-detects Bazzite / Fedora / Distrobox SD card mounts (/run/media/$USER, /run/host/run/media/$USER)
- Auto-detects xpwn's hfsplus-linux-x86_64 binary from ios3ds
- Inspects .ipa bundles before installation (CFBundleIdentifier, MinimumOSVersion, Mach-O ARMv6 slices, FairPlay DRM)
- Batch-installs .ipa packages into /Applications inside rootfs.hfs
- Lists and removes installed .app bundles from rootfs.hfs
- Clears SpringBoard icon caches and flushes Linux filesystem buffers (os.sync)
"""

import argparse
import getpass
import glob
import os
import plistlib
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import zipfile

# Mach-O constants
MH_MAGIC = 0xFEEDFACE
FAT_MAGIC = 0xCAFEBABE
FAT_CIGAM = 0xBEBAFECA
CPU_TYPE_ARM = 12
CPU_SUBTYPE_ARM_ALL = 0
CPU_SUBTYPE_ARM_V6 = 6
CPU_SUBTYPE_ARM_V7 = 9
LC_ENCRYPTION_INFO = 0x21

SUBTYPE_NAMES = {
    0: "arm_all",
    6: "armv6",
    9: "armv7",
    11: "armv7s",
}

STOCK_IOS4_APPS = {
    "AdSheet.app",
    "AppStore.app",
    "Calculator.app",
    "Contacts.app",
    "DemoApp.app",
    "FieldTest.app",
    "GameCenterUIService.app",
    "Install.app",
    "Maps.app",
    "MobileAddressBook.app",
    "MobileCal.app",
    "MobileMail.app",
    "MobileMusicPlayer.app",
    "MobileNotes.app",
    "MobilePhone.app",
    "MobileSMS.app",
    "MobileSafari.app",
    "MobileSlideShow.app",
    "MobileTimer.app",
    "Nike.app",
    "Preferences.app",
    "Setup.app",
    "Stocks.app",
    "TrustMe.app",
    "Videos.app",
    "VoiceMemos.app",
    "Weather.app",
    "Web.app",
    "WebSheet.app",
    "YouTube.app",
    "iPodOut.app",
    "iTunes.app",
}


def find_hfsplus_candidates(explicit_path=None):
    """Return a list of valid hfsplus binary paths."""
    here = os.path.dirname(os.path.abspath(__file__))
    home = os.path.expanduser("~")
    raw = []
    if explicit_path:
        raw.append(explicit_path)
    raw.extend([
        os.path.join(here, "prebuilt", "hfsplus-linux-x86_64"),
        os.path.join(here, "build", "tools", "hfsplus"),
        os.path.join(os.path.dirname(here), "prebuilt", "hfsplus-linux-x86_64"),
        os.path.join(os.path.dirname(here), "build", "tools", "hfsplus"),
    ])
    raw.extend(glob.glob(os.path.join(home, "**", "hfsplus-linux-x86_64"), recursive=False))
    raw.extend(glob.glob(os.path.join(home, "*", "*", "prebuilt", "hfsplus-linux-x86_64")))
    which_hfs = shutil.which("hfsplus") or shutil.which("hfsplus-linux-x86_64")
    if which_hfs:
        raw.append(which_hfs)

    found = []
    for c in raw:
        if c and os.path.isfile(c):
            try:
                if not os.access(c, os.X_OK):
                    os.chmod(c, 0o755)
            except OSError:
                pass
            p = os.path.abspath(c)
            if p not in found and os.access(p, os.X_OK):
                found.append(p)
    return found


def find_rootfs_candidates(explicit_target=None):
    """Auto-detect rootfs.hfs across Bazzite / Distrobox mounts and local build dirs."""
    user = getpass.getuser()
    here = os.path.dirname(os.path.abspath(__file__))
    home = os.path.expanduser("~")
    search_roots = []

    if explicit_target:
        search_roots.append(explicit_target)

    env_sd = os.environ.get("SD_MOUNT")
    if env_sd:
        search_roots.append(env_sd)

    # Bazzite & Distrobox removable media paths
    for media_base in [
        f"/run/media/{user}",
        f"/run/host/run/media/{user}",
        "/run/media",
        "/run/host/run/media",
        "/media",
        "/mnt",
    ]:
        if os.path.isdir(media_base):
            try:
                for entry in os.listdir(media_base):
                    sub = os.path.join(media_base, entry)
                    if os.path.isdir(sub):
                        search_roots.append(sub)
                        # Check one level deeper in case of /run/media/<user>/<label>
                        try:
                            for sub_entry in os.listdir(sub):
                                sub2 = os.path.join(sub, sub_entry)
                                if os.path.isdir(sub2):
                                    search_roots.append(sub2)
                        except OSError:
                            pass
            except OSError:
                pass

    search_roots.extend([
        here,
        os.path.dirname(here),
        os.path.join(home, "3ds-dev"),
    ])

    found = []
    for root in search_roots:
        if not root:
            continue
        p = os.path.abspath(root)
        if os.path.isfile(p) and p.endswith((".hfs", ".img", ".dmg")):
            if p not in found:
                found.append(p)
        elif os.path.isdir(p):
            for rel in [
                os.path.join("ios3ds", "rootfs.hfs"),
                "rootfs.hfs",
                os.path.join("ios3ds-sd", "ios3ds", "rootfs.hfs"),
                os.path.join("build", "sd", "ios3ds", "rootfs.hfs"),
            ]:
                cand = os.path.join(p, rel)
                if os.path.isfile(cand):
                    ap = os.path.abspath(cand)
                    if ap not in found:
                        found.append(ap)
    return found


def hfs_run(hfs_bin, img_path, *args, check=True):
    """Run an hfsplus command against rootfs.hfs."""
    cmd = [hfs_bin, img_path] + list(args)
    res = subprocess.run(cmd, capture_output=True, text=True)
    if check and res.returncode != 0:
        err = res.stderr.strip() or res.stdout.strip()
        raise RuntimeError(f"hfsplus {' '.join(args)} failed: {err}")
    return res.stdout


def hfs_exists(hfs_bin, img_path, remote_path):
    """Check if a file or directory exists inside rootfs.hfs."""
    parent, _, name = remote_path.rstrip("/").rpartition("/")
    out = hfs_run(hfs_bin, img_path, "ls", parent or "/", check=False)
    for line in out.splitlines():
        parts = line.split()
        if parts and parts[-1] == name:
            return True
    return False


def parse_macho_slice(blob, offset=0):
    """Parse a single 32-bit Mach-O header and check load commands for FairPlay DRM."""
    if len(blob) < offset + 28:
        return None
    magic, cputype, cpusubtype, filetype, ncmds, sizeofcmds, flags = struct.unpack_from(
        "<7I", blob, offset
    )
    if magic != MH_MAGIC:
        return None
    cpusubtype &= 0x00FFFFFF
    cryptid = 0
    cmd_off = offset + 28
    for _ in range(ncmds):
        if cmd_off + 8 > len(blob):
            break
        cmd, cmdsize = struct.unpack_from("<2I", blob, cmd_off)
        if cmd == LC_ENCRYPTION_INFO and cmd_off + 20 <= len(blob):
            _, _, cryptid = struct.unpack_from("<3I", blob, cmd_off + 8)
        if cmdsize == 0:
            break
        cmd_off += cmdsize
    return {
        "cputype": cputype,
        "cpusubtype": cpusubtype,
        "subtype_name": SUBTYPE_NAMES.get(cpusubtype, f"subtype_{cpusubtype}"),
        "cryptid": cryptid,
    }


def inspect_macho(binary_path):
    """Inspect a Thin or Fat Mach-O binary for ARMv6 support and encryption state."""
    with open(binary_path, "rb") as f:
        blob = f.read(65536)
    if len(blob) < 28:
        raise ValueError("Executable is too small to be a valid Mach-O binary.")

    magic_be = struct.unpack_from(">I", blob, 0)[0]
    magic_le = struct.unpack_from("<I", blob, 0)[0]
    slices = []

    if magic_be == FAT_MAGIC or magic_le == FAT_CIGAM:
        nfat_arch = struct.unpack_from(">I", blob, 4)[0]
        for i in range(nfat_arch):
            cputype, cpusubtype, offset, size, align = struct.unpack_from(
                ">5I", blob, 8 + i * 20
            )
            cpusubtype &= 0x00FFFFFF
            with open(binary_path, "rb") as f:
                f.seek(offset)
                sub_blob = f.read(32768)
            info = parse_macho_slice(sub_blob, 0)
            if info:
                slices.append(info)
            else:
                slices.append({
                    "cputype": cputype,
                    "cpusubtype": cpusubtype,
                    "subtype_name": SUBTYPE_NAMES.get(cpusubtype, f"subtype_{cpusubtype}"),
                    "cryptid": 0,
                })
    elif magic_le == MH_MAGIC:
        info = parse_macho_slice(blob, 0)
        if info:
            slices.append(info)
    else:
        raise ValueError(f"Unrecognized binary format (magic: {magic_be:#010x}).")

    return slices


def parse_version(ver_str):
    parts = []
    for x in str(ver_str).strip().split("."):
        digits = "".join(ch for ch in x if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def load_plist(plist_path):
    with open(plist_path, "rb") as f:
        data = f.read()
    return plistlib.loads(data)


def inspect_ipa_metadata(ipa_path):
    """Quickly inspect an .ipa archive without extracting the full bundle to disk."""
    meta = {
        "path": ipa_path,
        "filename": os.path.basename(ipa_path),
        "bundle_name": "?",
        "bundle_id": "?",
        "min_os": "2.0",
        "arch": "?",
        "status": "Ready",
        "valid_armv6": False,
        "encrypted": False,
    }
    try:
        with zipfile.ZipFile(ipa_path, "r") as zf:
            names = zf.namelist()
            plist_entries = [
                n for n in names
                if n.endswith(".app/Info.plist") and n.count(".app/") == 1
            ]
            if not plist_entries:
                meta["status"] = "Invalid (No Info.plist)"
                return meta
            plist_entry = plist_entries[0]
            app_prefix = plist_entry[:-len("Info.plist")]
            meta["bundle_name"] = os.path.basename(app_prefix.rstrip("/"))

            info = plistlib.loads(zf.read(plist_entry))
            exec_name = info.get("CFBundleExecutable") or os.path.splitext(meta["bundle_name"])[0]
            meta["bundle_id"] = info.get("CFBundleIdentifier", "unknown")
            meta["min_os"] = str(info.get("MinimumOSVersion", "2.0"))

            exec_entry = app_prefix + exec_name
            if exec_entry in names:
                with tempfile.NamedTemporaryFile(delete=False) as tf:
                    with zf.open(exec_entry) as zf_exec:
                        tf.write(zf_exec.read(262144))
                    tmp_exec = tf.name
                try:
                    with open(tmp_exec, "rb") as bf:
                        blob = bf.read(65536)
                    magic_be = struct.unpack_from(">I", blob, 0)[0]
                    magic_le = struct.unpack_from("<I", blob, 0)[0]
                    slices = []
                    if magic_be == FAT_MAGIC or magic_le == FAT_CIGAM:
                        nfat = struct.unpack_from(">I", blob, 4)[0]
                        for i in range(nfat):
                            ct, cs, off, sz, _ = struct.unpack_from(">5I", blob, 8 + i * 20)
                            cs &= 0x00FFFFFF
                            slices.append({
                                "cputype": ct,
                                "cpusubtype": cs,
                                "subtype_name": SUBTYPE_NAMES.get(cs, f"sub_{cs}"),
                                "cryptid": 0,
                            })
                    elif magic_le == MH_MAGIC:
                        s = parse_macho_slice(blob, 0)
                        if s:
                            slices.append(s)

                    meta["arch"] = ", ".join(s["subtype_name"] for s in slices) or "unknown"
                    armv6 = [
                        s for s in slices
                        if s["cputype"] == CPU_TYPE_ARM and s["cpusubtype"] in (CPU_SUBTYPE_ARM_ALL, CPU_SUBTYPE_ARM_V6)
                    ]
                    meta["valid_armv6"] = bool(armv6)
                    meta["encrypted"] = any(s.get("cryptid", 0) != 0 for s in armv6)
                    if not meta["valid_armv6"]:
                        meta["status"] = f"WARN: No armv6 ({meta['arch']})"
                    elif meta["encrypted"]:
                        meta["status"] = "WARN: FairPlay Encrypted"
                    elif parse_version(meta["min_os"]) > (4, 2, 1):
                        meta["status"] = f"Ready (Auto-patch iOS {meta['min_os']}->4.2.1)"
                    else:
                        meta["status"] = "Ready (ARMv6 OK)"
                finally:
                    try:
                        os.remove(tmp_exec)
                    except OSError:
                        pass
            else:
                meta["status"] = "Missing executable"
    except Exception as e:
        meta["status"] = f"Error: {e}"
    return meta


def install_single_ipa(ipa_path, hfs_bin, rootfs_path, force=False, patch_min_os=True, log_fn=print):
    """Extract, validate, and inject a single .ipa file into rootfs.hfs."""
    if not os.path.isfile(ipa_path):
        raise RuntimeError(f"File not found: {ipa_path}")

    log_fn(f"\n==> Processing {os.path.basename(ipa_path)}")
    with tempfile.TemporaryDirectory(prefix="ios3ds_ipa_") as tmpdir:
        try:
            with zipfile.ZipFile(ipa_path, "r") as zf:
                zf.extractall(tmpdir)
        except zipfile.BadZipFile:
            raise RuntimeError(f"'{ipa_path}' is not a valid ZIP/.ipa archive.")

        payload_dir = os.path.join(tmpdir, "Payload")
        if not os.path.isdir(payload_dir):
            apps = []
            for root, dirs, _ in os.walk(tmpdir):
                for d in dirs:
                    if d.endswith(".app"):
                        apps.append(os.path.join(root, d))
            if not apps:
                raise RuntimeError(f"No Payload/*.app bundle found inside '{ipa_path}'.")
            app_dir = apps[0]
        else:
            apps = [
                os.path.join(payload_dir, d)
                for d in os.listdir(payload_dir)
                if d.endswith(".app") and os.path.isdir(os.path.join(payload_dir, d))
            ]
            if not apps:
                raise RuntimeError(f"Payload/ folder in '{ipa_path}' contains no .app directory.")
            app_dir = apps[0]

        app_bundle_name = os.path.basename(app_dir)
        info_plist_path = os.path.join(app_dir, "Info.plist")
        if not os.path.isfile(info_plist_path):
            raise RuntimeError(f"Missing Info.plist inside {app_bundle_name}")

        info = load_plist(info_plist_path)
        exec_name = info.get("CFBundleExecutable") or os.path.splitext(app_bundle_name)[0]
        bundle_id = info.get("CFBundleIdentifier", "unknown.bundle.id")
        min_os = info.get("MinimumOSVersion", "2.0")

        log_fn(f"    App Bundle : {app_bundle_name} ({bundle_id})")
        log_fn(f"    Executable : {exec_name}")
        log_fn(f"    Min iOS Ver: {min_os}")

        if parse_version(min_os) > (4, 2, 1):
            if patch_min_os:
                log_fn(f"    [!] MinimumOSVersion is {min_os} (> 4.2.1); patching Info.plist to 4.2.1")
                info["MinimumOSVersion"] = "4.2.1"
                with open(info_plist_path, "wb") as f:
                    plistlib.dump(info, f, fmt=plistlib.FMT_XML)
            elif not force:
                raise RuntimeError(
                    f"App requires iOS {min_os} (ios3ds runs iOS 4.2.1). Enable Force or Auto-Patch."
                )

        exec_path = os.path.join(app_dir, exec_name)
        if not os.path.isfile(exec_path):
            raise RuntimeError(f"Main executable '{exec_name}' not found inside {app_bundle_name}")

        try:
            slices = inspect_macho(exec_path)
        except ValueError as e:
            if not force:
                raise RuntimeError(str(e))
            slices = []

        if slices:
            slice_desc = ", ".join(
                f"{s['subtype_name']} ({'encrypted' if s['cryptid'] else 'decrypted'})"
                for s in slices
            )
            log_fn(f"    Mach-O Arch: {slice_desc}")

            armv6_slices = [
                s for s in slices
                if s["cputype"] == CPU_TYPE_ARM and s["cpusubtype"] in (CPU_SUBTYPE_ARM_ALL, CPU_SUBTYPE_ARM_V6)
            ]
            if not armv6_slices:
                msg = (
                    f"{exec_name} only contains {[s['subtype_name'] for s in slices]} slices! "
                    "The 3DS ARM11 CPU requires an armv6 slice (armv7-only binaries crash with SIGILL)."
                )
                if not force:
                    raise RuntimeError(f"{msg}\nEnable 'Force Install' to bypass this check.")
                log_fn(f"    [WARNING] {msg}")
            elif any(s["cryptid"] != 0 for s in armv6_slices):
                msg = (
                    f"{exec_name} has FairPlay DRM encryption enabled (cryptid != 0). "
                    "ios3ds requires decrypted ('cracked') IPA binaries."
                )
                if not force:
                    raise RuntimeError(f"{msg}\nEnable 'Force Install' to bypass this check.")
                log_fn(f"    [WARNING] {msg}")

        code_sig_dir = os.path.join(app_dir, "_CodeSignature")
        if os.path.isdir(code_sig_dir):
            shutil.rmtree(code_sig_dir, ignore_errors=True)

        remote_app_dir = f"/Applications/{app_bundle_name}"
        if hfs_exists(hfs_bin, rootfs_path, remote_app_dir):
            log_fn(f"    Removing existing {remote_app_dir} from rootfs.hfs...")
            hfs_run(hfs_bin, rootfs_path, "rmall", remote_app_dir, check=False)
            hfs_run(hfs_bin, rootfs_path, "rm", remote_app_dir, check=False)

        log_fn(f"    Injecting {app_bundle_name} into {remote_app_dir}...")
        hfs_run(hfs_bin, rootfs_path, "mkdir", remote_app_dir)
        hfs_run(hfs_bin, rootfs_path, "addall", app_dir, remote_app_dir)

        remote_exec = f"{remote_app_dir}/{exec_name}"
        hfs_run(hfs_bin, rootfs_path, "chmod", "0755", remote_exec)

        for root, _, files in os.walk(app_dir):
            for fname in files:
                fpath = os.path.join(root, fname)
                rel = os.path.relpath(fpath, app_dir).replace("\\", "/")
                if rel == exec_name:
                    continue
                try:
                    with open(fpath, "rb") as bf:
                        hdr = bf.read(4)
                    if hdr in (b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca") or hdr.startswith(b"#!"):
                        hfs_run(hfs_bin, rootfs_path, "chmod", "0755", f"{remote_app_dir}/{rel}", check=False)
                except OSError:
                    pass

        log_fn(f"    [+] Installed {app_bundle_name} -> {remote_app_dir}")


def clear_springboard_caches(hfs_bin, rootfs_path, log_fn=print):
    """Remove SpringBoard icon/application caches so new apps appear on next boot."""
    cache_paths = [
        "/private/var/mobile/Library/Caches/com.apple.mobile.installation.plist",
        "/private/var/mobile/Library/Caches/com.apple.IconsCache",
        "/private/var/mobile/Library/Caches/com.apple.SpringBoard.folderSwitcherLinen",
    ]
    for cp in cache_paths:
        if hfs_exists(hfs_bin, rootfs_path, cp):
            hfs_run(hfs_bin, rootfs_path, "rm", cp, check=False)
            log_fn(f"    Cleared cache: {cp}")


def get_installed_apps(hfs_bin, rootfs_path):
    """Return a list of (app_name, is_stock) tuples from /Applications."""
    out = hfs_run(hfs_bin, rootfs_path, "ls", "/Applications")
    apps = []
    for line in out.splitlines():
        parts = line.split()
        if parts and parts[-1].endswith(".app"):
            name = parts[-1]
            apps.append((name, name in STOCK_IOS4_APPS))
    return sorted(apps, key=lambda x: (x[1], x[0].lower()))


# ==============================================================================
# Tkinter GUI Implementation
# ==============================================================================

def launch_gui(initial_target=None, initial_hfsplus=None, initial_ipas=None):
    try:
        import tkinter as tk
        from tkinter import ttk, filedialog, messagebox
        from tkinter.scrolledtext import ScrolledText
    except ImportError:
        sys.stderr.write(
            "ERROR: Python tkinter is not installed in this container/host.\n"
            "On Fedora/Bazzite Distrobox, install it with:\n"
            "  sudo dnf install -y python3-tkinter\n"
            "Or run in CLI mode: python3 install_ipa_gui.py <SD_MOUNT> <app.ipa>\n"
        )
        sys.exit(1)

    class Ios3dsIpaApp(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title("ios3ds IPA Installer & App Manager (iOS 4.2.1 / ARMv6)")
            self.geometry("920x680")
            self.minsize(780, 560)
            self.configure(bg="#181825")

            self.style = ttk.Style(self)
            try:
                self.style.theme_use("clam")
            except tk.TclError:
                pass

            # Catppuccin Mocha-inspired dark palette
            bg_main = "#181825"
            bg_panel = "#1e1e2e"
            bg_input = "#313244"
            fg_text = "#cdd6f4"
            accent = "#89b4fa"
            accent_active = "#b4befe"

            self.style.configure(".", background=bg_main, foreground=fg_text, font=("Sans", 10))
            self.style.configure("TFrame", background=bg_main)
            self.style.configure("TLabelframe", background=bg_panel, foreground=accent, borderwidth=1)
            self.style.configure("TLabelframe.Label", background=bg_panel, foreground=accent, font=("Sans", 10, "bold"))
            self.style.configure("TLabel", background=bg_panel, foreground=fg_text)
            self.style.configure("Header.TLabel", background=bg_main, foreground=accent, font=("Sans", 13, "bold"))
            self.style.configure("Sub.TLabel", background=bg_main, foreground="#a6adc8", font=("Sans", 9))
            self.style.configure("TCheckbutton", background=bg_panel, foreground=fg_text)
            self.style.map("TCheckbutton", background=[("active", bg_panel)])
            self.style.configure("TEntry", fieldbackground=bg_input, foreground=fg_text, insertcolor=fg_text)
            self.style.configure(
                "TButton",
                background=bg_input,
                foreground=fg_text,
                padding=(10, 5),
                borderwidth=1,
            )
            self.style.map(
                "TButton",
                background=[("active", "#45475a"), ("disabled", "#11111b")],
                foreground=[("disabled", "#585b70")],
            )
            self.style.configure(
                "Accent.TButton",
                background="#2563eb",
                foreground="#ffffff",
                font=("Sans", 10, "bold"),
                padding=(14, 6),
            )
            self.style.map(
                "Accent.TButton",
                background=[("active", "#3b82f6"), ("disabled", "#1e3a8a")],
            )
            self.style.configure(
                "Treeview",
                background=bg_input,
                fieldbackground=bg_input,
                foreground=fg_text,
                rowheight=26,
                borderwidth=0,
            )
            self.style.configure(
                "Treeview.Heading",
                background="#45475a",
                foreground="#ffffff",
                font=("Sans", 9, "bold"),
            )
            self.style.map("Treeview", background=[("selected", "#2563eb")])

            self.rootfs_var = tk.StringVar()
            self.hfsplus_var = tk.StringVar()
            self.patch_os_var = tk.BooleanVar(value=True)
            self.force_var = tk.BooleanVar(value=False)
            self.hide_stock_var = tk.BooleanVar(value=True)
            self.status_var = tk.StringVar(value="Ready.")
            self.ipa_queue = []  # List of dicts from inspect_ipa_metadata
            self.busy = False

            self._build_ui()
            self._auto_detect_paths(initial_target, initial_hfsplus)
            if initial_ipas:
                for p in initial_ipas:
                    if os.path.isfile(p):
                        self._add_ipa_path(os.path.abspath(p))

        def _build_ui(self):
            # Header
            hdr_frame = ttk.Frame(self)
            hdr_frame.pack(fill="x", padx=14, pady=(12, 6))
            ttk.Label(
                hdr_frame,
                text="ios3ds IPA Sideloader & HFS+ App Manager",
                style="Header.TLabel",
            ).pack(anchor="w")
            ttk.Label(
                hdr_frame,
                text="Validates ARMv6 slices & FairPlay DRM, injects .ipa bundles into rootfs.hfs, and clears SpringBoard caches.",
                style="Sub.TLabel",
            ).pack(anchor="w")

            # Paths Configuration Frame
            paths_frame = ttk.LabelFrame(self, text=" 1. Target HFS+ Image & Tool Paths ", padding=10)
            paths_frame.pack(fill="x", padx=14, pady=6)
            paths_frame.columnconfigure(1, weight=1)

            ttk.Label(paths_frame, text="Target rootfs.hfs:").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
            ttk.Entry(paths_frame, textvariable=self.rootfs_var).grid(row=0, column=1, sticky="ew", pady=4)
            ttk.Button(paths_frame, text="Browse File...", command=self._browse_rootfs).grid(row=0, column=2, padx=4, pady=4)
            ttk.Button(paths_frame, text="SD Folder...", command=self._browse_sd_dir).grid(row=0, column=3, padx=4, pady=4)
            ttk.Button(paths_frame, text="Auto-Detect", command=self._auto_detect_paths).grid(row=0, column=4, padx=(4, 0), pady=4)

            ttk.Label(paths_frame, text="hfsplus Binary:").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
            ttk.Entry(paths_frame, textvariable=self.hfsplus_var).grid(row=1, column=1, sticky="ew", pady=4)
            ttk.Button(paths_frame, text="Browse...", command=self._browse_hfsplus).grid(row=1, column=2, columnspan=3, sticky="ew", padx=4, pady=4)

            # Notebook Tabs: Queue vs Installed Apps
            self.notebook = ttk.Notebook(self)
            self.notebook.pack(fill="both", expand=True, padx=14, pady=6)

            # Tab 1: IPA Install Queue
            tab_install = ttk.Frame(self.notebook, padding=8)
            self.notebook.add(tab_install, text="  Install .IPA Packages  ")

            tree_frame = ttk.Frame(tab_install)
            tree_frame.pack(fill="both", expand=True)

            cols = ("filename", "bundle_name", "min_os", "arch", "status")
            self.ipa_tree = ttk.Treeview(tree_frame, columns=cols, show="headings", selectmode="extended")
            self.ipa_tree.heading("filename", text="IPA File")
            self.ipa_tree.heading("bundle_name", text="App Bundle")
            self.ipa_tree.heading("min_os", text="Min iOS")
            self.ipa_tree.heading("arch", text="Mach-O Slices")
            self.ipa_tree.heading("status", text="Compatibility Status")

            self.ipa_tree.column("filename", width=190, anchor="w")
            self.ipa_tree.column("bundle_name", width=150, anchor="w")
            self.ipa_tree.column("min_os", width=70, anchor="center")
            self.ipa_tree.column("arch", width=130, anchor="center")
            self.ipa_tree.column("status", width=260, anchor="w")

            sb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.ipa_tree.yview)
            self.ipa_tree.configure(yscrollcommand=sb.set)
            self.ipa_tree.pack(side="left", fill="both", expand=True)
            sb.pack(side="right", fill="y")

            ctrl_frame = ttk.Frame(tab_install)
            ctrl_frame.pack(fill="x", pady=(8, 2))

            ttk.Button(ctrl_frame, text="+ Add .IPA Files...", command=self._add_ipas_dialog).pack(side="left", padx=(0, 6))
            ttk.Button(ctrl_frame, text="Remove Selected", command=self._remove_selected_ipas).pack(side="left", padx=6)
            ttk.Button(ctrl_frame, text="Clear Queue", command=self._clear_ipa_queue).pack(side="left", padx=6)

            self.install_btn = ttk.Button(
                ctrl_frame,
                text="Install Queued IPAs to rootfs.hfs",
                style="Accent.TButton",
                command=self._start_install_thread,
            )
            self.install_btn.pack(side="right")

            opts_frame = ttk.Frame(tab_install)
            opts_frame.pack(fill="x", pady=(4, 0))
            ttk.Checkbutton(
                opts_frame,
                text="Auto-patch MinimumOSVersion > 4.2.1 in Info.plist",
                variable=self.patch_os_var,
            ).pack(side="left", padx=(0, 16))
            ttk.Checkbutton(
                opts_frame,
                text="Force install (ignore missing ARMv6 or FairPlay encryption warnings)",
                variable=self.force_var,
            ).pack(side="left")

            # Tab 2: Installed Apps inside rootfs.hfs
            tab_manage = ttk.Frame(self.notebook, padding=8)
            self.notebook.add(tab_manage, text="  Manage Installed Apps (/Applications)  ")

            mgr_top = ttk.Frame(tab_manage)
            mgr_top.pack(fill="x", pady=(0, 6))
            ttk.Button(mgr_top, text="Refresh Installed Apps", command=self._refresh_installed_apps).pack(side="left", padx=(0, 10))
            ttk.Checkbutton(
                mgr_top,
                text="Hide stock iOS 4.2.1 system apps",
                variable=self.hide_stock_var,
                command=self._refresh_installed_apps,
            ).pack(side="left")
            self.uninstall_btn = ttk.Button(
                mgr_top,
                text="Uninstall Selected App",
                command=self._uninstall_selected_app,
            )
            self.uninstall_btn.pack(side="right")

            mgr_tree_frame = ttk.Frame(tab_manage)
            mgr_tree_frame.pack(fill="both", expand=True)
            self.installed_tree = ttk.Treeview(
                mgr_tree_frame,
                columns=("app_name", "type"),
                show="headings",
                selectmode="browse",
            )
            self.installed_tree.heading("app_name", text="Application Bundle (/Applications)")
            self.installed_tree.heading("type", text="Category")
            self.installed_tree.column("app_name", width=480, anchor="w")
            self.installed_tree.column("type", width=200, anchor="center")

            mgr_sb = ttk.Scrollbar(mgr_tree_frame, orient="vertical", command=self.installed_tree.yview)
            self.installed_tree.configure(yscrollcommand=mgr_sb.set)
            self.installed_tree.pack(side="left", fill="both", expand=True)
            mgr_sb.pack(side="right", fill="y")

            # Bottom Console Log
            log_frame = ttk.LabelFrame(self, text=" Activity & Verification Log ", padding=6)
            log_frame.pack(fill="both", expand=False, padx=14, pady=(2, 10))

            self.log_box = ScrolledText(
                log_frame,
                height=8,
                bg="#11111b",
                fg="#a6e3a1",
                insertbackground="#cdd6f4",
                font=("Monospace", 9),
                relief="flat",
                state="disabled",
            )
            self.log_box.pack(fill="both", expand=True)

            status_bar = ttk.Frame(self)
            status_bar.pack(fill="x", padx=14, pady=(0, 8))
            ttk.Label(status_bar, textvariable=self.status_var, style="Sub.TLabel").pack(side="left")
            ttk.Button(status_bar, text="Sync Disk Buffers (os.sync)", command=self._manual_sync).pack(side="right")

        def log(self, msg):
            def _append():
                self.log_box.configure(state="normal")
                self.log_box.insert("end", str(msg) + "\n")
                self.log_box.see("end")
                self.log_box.configure(state="disabled")
            self.after(0, _append)

        def _auto_detect_paths(self, explicit_target=None, explicit_hfsplus=None):
            hfs_cands = find_hfsplus_candidates(explicit_hfsplus)
            if hfs_cands:
                self.hfsplus_var.set(hfs_cands[0])
                self.log(f"[*] Detected hfsplus tool: {hfs_cands[0]}")
            else:
                self.log("[!] Could not auto-detect prebuilt/hfsplus-linux-x86_64. Please browse to it.")

            rootfs_cands = find_rootfs_candidates(explicit_target)
            if rootfs_cands:
                self.rootfs_var.set(rootfs_cands[0])
                self.log(f"[*] Detected target rootfs: {rootfs_cands[0]}")
                self._refresh_installed_apps()
            else:
                self.log("[!] Could not auto-detect ios3ds/rootfs.hfs. Mount your 3DS SD card and click Auto-Detect or Browse.")

        def _browse_rootfs(self):
            p = filedialog.askopenfilename(
                title="Select ios3ds rootfs.hfs Image",
                filetypes=[("HFS+ Rootfs Image", "*.hfs *.img *.dmg"), ("All Files", "*.*")],
            )
            if p:
                self.rootfs_var.set(p)
                self._refresh_installed_apps()

        def _browse_sd_dir(self):
            d = filedialog.askdirectory(title="Select Mounted 3DS SD Card Folder")
            if d:
                cands = find_rootfs_candidates(d)
                if cands:
                    self.rootfs_var.set(cands[0])
                    self.log(f"[*] Found rootfs image inside SD card: {cands[0]}")
                    self._refresh_installed_apps()
                else:
                    messagebox.showwarning(
                        "rootfs.hfs Not Found",
                        f"Could not find ios3ds/rootfs.hfs inside:\n{d}",
                    )

        def _browse_hfsplus(self):
            p = filedialog.askopenfilename(
                title="Select prebuilt/hfsplus-linux-x86_64 Binary",
                filetypes=[("Executable", "hfsplus*"), ("All Files", "*.*")],
            )
            if p:
                self.hfsplus_var.set(p)

        def _add_ipa_path(self, p):
            if any(item["path"] == p for item in self.ipa_queue):
                return
            meta = inspect_ipa_metadata(p)
            self.ipa_queue.append(meta)
            self.ipa_tree.insert(
                "",
                "end",
                iid=p,
                values=(
                    meta["filename"],
                    f"{meta['bundle_name']} ({meta['bundle_id']})",
                    meta["min_os"],
                    meta["arch"],
                    meta["status"],
                ),
            )
            self.log(f"[+] Queued {meta['filename']} -> {meta['bundle_name']} [{meta['status']}]")

        def _add_ipas_dialog(self):
            files = filedialog.askopenfilenames(
                title="Select Decrypted ARMv6 .IPA Files",
                filetypes=[("iOS App Package (.ipa)", "*.ipa *.IPA *.zip"), ("All Files", "*.*")],
            )
            for f in files:
                self._add_ipa_path(os.path.abspath(f))

        def _remove_selected_ipas(self):
            for iid in self.ipa_tree.selection():
                self.ipa_tree.delete(iid)
                self.ipa_queue = [x for x in self.ipa_queue if x["path"] != iid]

        def _clear_ipa_queue(self):
            for iid in self.ipa_tree.get_children():
                self.ipa_tree.delete(iid)
            self.ipa_queue.clear()

        def _validate_tools(self):
            hfs_bin = self.hfsplus_var.get().strip()
            rootfs_path = self.rootfs_var.get().strip()
            if not hfs_bin or not os.path.isfile(hfs_bin):
                messagebox.showerror("Missing hfsplus Tool", "Please select a valid hfsplus-linux-x86_64 binary.")
                return None, None
            if not rootfs_path or not os.path.isfile(rootfs_path):
                messagebox.showerror("Missing rootfs.hfs", "Please select a valid ios3ds/rootfs.hfs image on your SD card.")
                return None, None
            return hfs_bin, rootfs_path

        def _start_install_thread(self):
            if self.busy:
                return
            hfs_bin, rootfs_path = self._validate_tools()
            if not hfs_bin:
                return
            if not self.ipa_queue:
                messagebox.showinfo("Empty Queue", "Add at least one .ipa file to install first.")
                return

            self.busy = True
            self.install_btn.configure(state="disabled")
            self.status_var.set("Installing IPA packages into rootfs.hfs...")

            ipas_to_install = [x["path"] for x in self.ipa_queue]
            force = self.force_var.get()
            patch_os = self.patch_os_var.get()

            def _worker():
                ok_count = 0
                fail_count = 0
                for ipa_path in ipas_to_install:
                    try:
                        install_single_ipa(
                            ipa_path,
                            hfs_bin,
                            rootfs_path,
                            force=force,
                            patch_min_os=patch_os,
                            log_fn=self.log,
                        )
                        ok_count += 1
                        self.after(
                            0,
                            lambda p=ipa_path: self.ipa_tree.set(p, "status", "Installed OK"),
                        )
                    except Exception as e:
                        fail_count += 1
                        self.log(f"    [ERROR] {e}")
                        self.after(
                            0,
                            lambda p=ipa_path, err=str(e): self.ipa_tree.set(p, "status", f"FAILED: {err[:45]}"),
                        )

                if ok_count > 0:
                    try:
                        clear_springboard_caches(hfs_bin, rootfs_path, log_fn=self.log)
                        self.log("[*] Syncing Linux filesystem buffers (os.sync)...")
                        os.sync()
                        self.log(f"[+] Finished! Installed {ok_count} app(s) into {rootfs_path}.")
                    except Exception as e:
                        self.log(f"[!] Cache/sync warning: {e}")

                def _finish():
                    self.busy = False
                    self.install_btn.configure(state="normal")
                    self.status_var.set(f"Complete: {ok_count} installed, {fail_count} failed.")
                    self._refresh_installed_apps()
                    if fail_count == 0:
                        messagebox.showinfo(
                            "Installation Complete",
                            f"Successfully installed {ok_count} IPA package(s) into rootfs.hfs and synced disk buffers!",
                        )
                    else:
                        messagebox.showwarning(
                            "Completed with Errors",
                            f"Installed {ok_count} package(s), {fail_count} failed.\nCheck the Activity Log for details.",
                        )

                self.after(0, _finish)

            threading.Thread(target=_worker, daemon=True).start()

        def _refresh_installed_apps(self):
            hfs_bin = self.hfsplus_var.get().strip()
            rootfs_path = self.rootfs_var.get().strip()
            if not (hfs_bin and os.path.isfile(hfs_bin) and rootfs_path and os.path.isfile(rootfs_path)):
                return
            for iid in self.installed_tree.get_children():
                self.installed_tree.delete(iid)
            try:
                apps = get_installed_apps(hfs_bin, rootfs_path)
                hide_stock = self.hide_stock_var.get()
                for name, is_stock in apps:
                    if hide_stock and is_stock:
                        continue
                    cat = "Stock iOS 4.2.1 App" if is_stock else "User / Sideloaded App"
                    self.installed_tree.insert("", "end", iid=name, values=(name, cat))
            except Exception as e:
                self.log(f"[!] Could not list /Applications: {e}")

        def _uninstall_selected_app(self):
            if self.busy:
                return
            sel = self.installed_tree.selection()
            if not sel:
                messagebox.showinfo("Select an App", "Select an application from the list to uninstall.")
                return
            app_name = sel[0]
            if app_name in STOCK_IOS4_APPS:
                if not messagebox.askyesno(
                    "Warning: Stock iOS App",
                    f"'{app_name}' is a built-in iOS 4.2.1 system app.\nAre you sure you want to delete it from rootfs.hfs?",
                ):
                    return
            else:
                if not messagebox.askyesno(
                    "Confirm Uninstall",
                    f"Remove '/Applications/{app_name}' from rootfs.hfs?",
                ):
                    return

            hfs_bin, rootfs_path = self._validate_tools()
            if not hfs_bin:
                return

            remote_dir = f"/Applications/{app_name}"
            try:
                self.log(f"\n[*] Removing {remote_dir}...")
                hfs_run(hfs_bin, rootfs_path, "rmall", remote_dir, check=False)
                hfs_run(hfs_bin, rootfs_path, "rm", remote_dir, check=False)
                clear_springboard_caches(hfs_bin, rootfs_path, log_fn=self.log)
                os.sync()
                self.log(f"[+] Removed {app_name} and synced disk buffers.")
                self._refresh_installed_apps()
            except Exception as e:
                messagebox.showerror("Uninstall Failed", str(e))

        def _manual_sync(self):
            self.status_var.set("Syncing filesystem caches to SD card...")
            self.update_idletasks()
            os.sync()
            self.status_var.set("Disk buffers synced! Safe to unmount SD card.")
            self.log("[*] os.sync() completed.")

    app = Ios3dsIpaApp()
    app.mainloop()


# ==============================================================================
# Entrypoint (Defaults to GUI when invoked with no arguments or --gui)
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="GUI & CLI installer for decrypted ARMv6 .ipa packages into an ios3ds rootfs.hfs image."
    )
    parser.add_argument(
        "target",
        nargs="?",
        help="Optional path to mounted 3DS SD card or rootfs.hfs (launches GUI if omitted)",
    )
    parser.add_argument(
        "ipas",
        nargs="*",
        help="One or more .ipa files to install",
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Force launch the graphical user interface",
    )
    parser.add_argument(
        "--hfsplus",
        help="Explicit path to prebuilt/hfsplus-linux-x86_64",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List all .app bundles currently installed in /Applications (CLI mode)",
    )
    parser.add_argument(
        "--remove",
        metavar="APP_NAME",
        help="Remove an installed app from /Applications (CLI mode)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Install even if ARMv6 slice is missing or FairPlay encryption is detected",
    )
    parser.add_argument(
        "--no-patch-version",
        action="store_true",
        help="Do not automatically lower MinimumOSVersion > 4.2.1 in Info.plist",
    )

    args = parser.parse_args()

    # Launch GUI if --gui is passed, or if no CLI action/arguments were given
    if args.gui or (not args.target and not args.ipas and not args.list and not args.remove):
        launch_gui(
            initial_target=args.target,
            initial_hfsplus=args.hfsplus,
            initial_ipas=args.ipas,
        )
        return

    # If user passed only .ipa files as positional args (e.g. `python3 install_ipa_gui.py game.ipa`), open GUI with it queued
    if args.target and args.target.lower().endswith(".ipa") and not args.list and not args.remove:
        all_ipas = [args.target] + (args.ipas or [])
        launch_gui(
            initial_target=None,
            initial_hfsplus=args.hfsplus,
            initial_ipas=all_ipas,
        )
        return

    # CLI execution path
    hfs_cands = find_hfsplus_candidates(args.hfsplus)
    if not hfs_cands:
        raise SystemExit("ERROR: Could not find prebuilt/hfsplus-linux-x86_64. Pass --hfsplus /path/to/hfsplus-linux-x86_64")
    hfs_bin = hfs_cands[0]

    rootfs_cands = find_rootfs_candidates(args.target)
    if not rootfs_cands:
        raise SystemExit(f"ERROR: Could not find rootfs.hfs at '{args.target}'.")
    rootfs_path = rootfs_cands[0]

    print(f"[*] Using HFS+ tool : {hfs_bin}")
    print(f"[*] Target rootfs   : {rootfs_path}")

    if args.list:
        for name, is_stock in get_installed_apps(hfs_bin, rootfs_path):
            tag = "[stock]" if is_stock else "[user] "
            print(f"  {tag} {name}")
        return

    if args.remove:
        app_name = args.remove if args.remove.endswith(".app") else f"{args.remove}.app"
        remote_dir = f"/Applications/{app_name}"
        if not hfs_exists(hfs_bin, rootfs_path, remote_dir):
            raise SystemExit(f"ERROR: {remote_dir} does not exist in {rootfs_path}")
        print(f"[*] Removing {remote_dir}...")
        hfs_run(hfs_bin, rootfs_path, "rmall", remote_dir, check=False)
        hfs_run(hfs_bin, rootfs_path, "rm", remote_dir, check=False)
        clear_springboard_caches(hfs_bin, rootfs_path)
        os.sync()
        print(f"[+] Removed {app_name} and synced disk buffers.")
        return

    if not args.ipas:
        launch_gui(initial_target=rootfs_path, initial_hfsplus=hfs_bin)
        return

    for ipa in args.ipas:
        install_single_ipa(
            ipa,
            hfs_bin,
            rootfs_path,
            force=args.force,
            patch_min_os=not args.no_patch_version,
        )

    clear_springboard_caches(hfs_bin, rootfs_path)
    print("\n[*] Syncing filesystem buffers to disk...")
    os.sync()
    print("[+] Done! Safely unmount your SD card and boot ios3ds on your 3DS.")


if __name__ == "__main__":
    main()
