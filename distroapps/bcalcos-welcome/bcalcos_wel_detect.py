#bcalcos_wel_detect.py
#not standalone - see bcalcos_welcome
#v1.0

import subprocess
import os

# ============================================================
# PACKAGE MAPPING (display name → apt package)
# ============================================================

PACKAGE_MAP = {
    "Gnumeric": "gnumeric",
    "AbiWord": "abiword",
    "LibreOffice": "libreoffice",
    "Screenshot": "xfce4-screenshooter",
    "MousePad": "mousepad",
    "Marp": "marp",
    "mtpaint": "mtpaint",
    "Ristretto": "ristretto",
    "GIMP": "gimp",
    "OBS Studio": "obs-studio",
    "VLC": "vlc",
    "Audacity": "audacity",
    "GParted": "gparted",
    "BackinTime": "backintime-qt",
    "KeePassXC": "keepassxc",
    "gocryptfs": "gocryptfs",
    "ClamAV": "clamav",
    "WireGuard": "wireguard",
    "Task Manager": "xfce4-taskmanager",
    "Atril": "atril",
    "PDF Arranger": "pdfarranger",
    "Inkscape": "inkscape",
    "Darktable": "darktable",
}

# Binary paths for non-repo installations
BINARY_PATHS = {
    "Marp": "/usr/local/bin/marp",
}

# Documentation URLs (these stay as None status)
DOCS_URLS = {
    "Devuan Website": "https://www.devuan.org/os/community",
    "AppArmor Userdoc": "https://wiki.debian.org/AppArmor",
    "WireGuard Quick Start": "https://www.wireguard.com/quickstart/",
    "UFW Help Wiki": "https://wiki.debian.org/Uncomplicated%20Firewall%20%28ufw%29",
    "ClamAV Userdoc": "https://docs.clamav.net/",
}

# ============================================================
# DYNAMIC INSTALL STATUS DETECTION
# ============================================================

def check_package_installed(package_name):
    """
    Check if a package is installed using dpkg.
    Returns True if installed, False otherwise.
    """
    try:
        result = subprocess.run(
            ["dpkg", "-s", package_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        print(f"WARNING: dpkg timeout for '{package_name}'")
        return False
    except Exception as e:
        print(f"WARNING: dpkg error for '{package_name}': {e}")
        return False


def check_binary_exists(binary_path):
    """
    Check if a binary executable exists at a specific path and is executable.
    Used for non-repo installations like Marp.
    """
    try:
        return os.path.isfile(binary_path) and os.access(binary_path, os.X_OK)
    except Exception as e:
        print(f"WARNING: Binary check failed for '{binary_path}': {e}")
        return False


def detect_all_statuses(app_config):
    """
    Scan APPS_CONFIG and build a dictionary of actual installation states.
    System actions and documentation remain None.
    """
    status_map = {}

    for category, apps in app_config.items():
        for app_name, _ in apps:
            # System actions stay as None
            if app_name in ("Run Updates", "Create New User", "Keyboard Layout", "Settings"):
                status_map[app_name] = None
            
            # Documentation stay as None
            elif app_name in DOCS_URLS:
                status_map[app_name] = None
            
            # Binary installs (Marp)
            elif app_name in BINARY_PATHS:
                status_map[app_name] = check_binary_exists(BINARY_PATHS[app_name])
            
            # Regular apt packages
            elif app_name in PACKAGE_MAP:
                pkg = PACKAGE_MAP[app_name]
                status_map[app_name] = check_package_installed(pkg)
            
            # Unknown app
            else:
                print(f"WARNING: Unknown app '{app_name}' - assuming not installed")
                status_map[app_name] = False

    return status_map