#!/usr/bin/env python3
"""Application status detection for the BcalcOS Welcome Center.
v1.5
This module is imported by bcalcos_welcome.py; it is not a standalone program.
"""

import os
import subprocess


# Display name -> APT package name. Keep display names synchronized with
# APPS_CONFIG in bcalcos_welcome.py.
PACKAGE_MAP = {
    # Office
    "Gnumeric": "gnumeric",
    "AbiWord": "abiword",
    "LibreOffice": "libreoffice",
    "QOwnNotes": "qownnotes",
    "Atril": "atril",
    "PDF Arranger": "pdfarranger",
    "Xournal++": "xournalpp",

    # Communications
    "Claws Mail": "claws-mail",

    # Graphics
    "GIMP": "gimp",
    "mtPaint": "mtpaint",
    "Inkscape": "inkscape",
    "Darktable": "darktable",
    "XSane": "xsane",

    # Media
    "VLC": "vlc",
    "Audacity": "audacity",
    "OBS Studio": "obs-studio",
    "Flacon": "flacon",
    "HandBrake": "handbrake",
    "Ex Falso": "exfalso",
    "Quod Libet": "quodlibet",
    "Xfburn": "xfburn",

    # Files
    "BackInTime": "backintime-qt",
    "Grsync": "grsync",
    "Midnight Commander": "mc",

    # Security
    "KeePassXC": "keepassxc",
    "gocryptfs": "gocryptfs",
    "ClamAV": "clamav",
    "WireGuard": "wireguard",
    "Quail Scan": "bcalcos-extra",  # Executable is shipped by bcalcos-extra.
    "Quail Crypt": "bcalcos-extra",  # Executable is shipped by bcalcos-extra.

    # System
    "GParted": "gparted",
    "Task Manager": "xfce4-taskmanager",
    "htop": "htop",
    "Xterm": "xterm",
    "Quail System": "bcalcos-extra",  # Executable is shipped by bcalcos-extra.

    # Accessories
    "Screenshot": "xfce4-screenshooter",
    "MousePad": "mousepad",
    "Xfce Clipman": "xfce4-clipman-plugin",
    "Quail Calc": "bcalcos-base",  # Executable is shipped by bcalcos-base.
    "Quail Converter": "bcalcos-extra",  # Executable is shipped by bcalcos-extra.
    "Quail Reminder": "bcalcos-base",  # Executable is shipped by bcalcos-base.

    # Development
    "Geany": "geany",
    "Meld": "meld",
    "Vim": "vim",

    # Games
    "AisleRiot": "aisleriot",
}


# Applications shipped with BcalcOS that should be available and launchable,
# but must not expose a removal control in the Welcome Center.
NOT_REMOVABLE = frozenset({
    "Brave Origin",
    "Atril",
    "MousePad",
    "Quail Calc",
    "Quail Converter",
    "Quail Reminder",
    "Quail System",
    "Quail Scan",
    "Quail Crypt",
})


# System actions have no APT installation state.
SYSTEM_ACTIONS = frozenset({
    "Settings",
    "Run Updates",
    "Create New User",
    "Keyboard Layout",
    "Change Password",
})


# Documentation entries stay None in the detected status map.
DOCS_URLS = {
    "BcalcOS Website": "https://www.bcalcos.org/",
    "BcalcOS Repository": "https://github.com/briancalc/bcalcos",
    "Updating BcalcOS": "https://bcalcos.discourse.group/t/updating-bcalcos/28",
    "Creating a Regular User Account": "https://bcalcos.discourse.group/t/creating-a-regular-user-account/81",
    "The Firewall: UFW": "https://bcalcos.discourse.group/t/the-firewall-ufw/85",
    "AppArmor: App Security": "https://bcalcos.discourse.group/t/apparmor-app-security/86",
    "ClamAV: Scanning Your Home Folder": "https://bcalcos.discourse.group/t/clamav-scanning-your-home-folder/38",
    "gocryptfs: Protecting Your Files": "https://bcalcos.discourse.group/t/gocryptfs-protecting-your-files/39",
    "Grsync: Backing Up Your Files": "https://bcalcos.discourse.group/t/grsync-backing-up-your-files/106",
}


# Brave Origin ships with BcalcOS and has no APT package entry in this module.
# Check its executable directly instead of guessing the package name.
EXECUTABLE_CHECKS = {
    "Brave Origin": "/usr/bin/brave-origin-stable",
}


def check_package_installed(package_name):
    """Return True if dpkg reports the named package as installed."""
    if not package_name:
        return False

    try:
        result = subprocess.run(
            ["dpkg-query", "-W", "-f=${Status}", package_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
        return result.returncode == 0 and result.stdout.strip() == "install ok installed"
    except subprocess.TimeoutExpired:
        print(f"WARNING: dpkg timeout for '{package_name}'")
        return False
    except Exception as exc:
        print(f"WARNING: dpkg error for '{package_name}': {exc}")
        return False


def detect_all_statuses(app_config):
    """Return installation states for every configured application.

    True means installed, False means not installed, and None is used for
    system actions and documentation links that have no package state.
    """
    status_map = {}

    for _category, apps in app_config.items():
        for app_name, _configured_status in apps:
            if app_name in SYSTEM_ACTIONS or app_name in DOCS_URLS:
                status_map[app_name] = None
                continue

            if app_name in PACKAGE_MAP:
                status_map[app_name] = check_package_installed(PACKAGE_MAP[app_name])
                continue

            if app_name in EXECUTABLE_CHECKS:
                executable = EXECUTABLE_CHECKS[app_name]
                status_map[app_name] = os.path.isfile(executable) and os.access(executable, os.X_OK)
                continue

            print(f"WARNING: Unknown app '{app_name}' - assuming not installed")
            status_map[app_name] = False

    return status_map
