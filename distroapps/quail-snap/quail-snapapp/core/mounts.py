VIRTUAL_FS = (
    "proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "ramfs", "cgroup", "cgroup2",
    "pstore", "bpf", "tracefs", "debugfs", "configfs", "fusectl", "mqueue",
    "hugetlbfs", "efivarfs", "securityfs", "autofs", "binfmt_misc", "nsfs",
)
NETWORK_FS = (
    "nfs", "nfs4", "cifs", "smbfs", "sshfs", "fuse.sshfs", "ncpfs", "afs",
    "ceph", "glusterfs",
)


def _unescape(text):
    for code, char in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
        text = text.replace(code, char)
    return text


def read_mounts(path="/proc/mounts"):
    entries = []
    with open(path, "r") as fh:
        for line in fh:
            parts = line.split()
            if len(parts) < 4:
                continue
            entries.append({
                "device": _unescape(parts[0]),
                "mountpoint": _unescape(parts[1]),
                "fstype": parts[2],
                "options": parts[3].split(","),
            })
    return entries


def classify_mount(entry):
    mountpoint = entry["mountpoint"]
    fstype = entry["fstype"]
    if mountpoint == "/":
        return "root"
    if fstype in VIRTUAL_FS or fstype.startswith("cgroup"):
        return "virtual"
    if fstype in NETWORK_FS:
        return "network"
    if mountpoint == "/boot":
        return "boot"
    if mountpoint in ("/boot/efi", "/efi"):
        return "efi"
    if mountpoint == "/home":
        return "home"
    return "other"


def list_mounts(path="/proc/mounts"):
    return [(classify_mount(entry), entry) for entry in read_mounts(path)]


def foreign_mounts(path="/proc/mounts"):
    entries = read_mounts(path)
    root_device = next((e["device"] for e in entries if e["mountpoint"] == "/"), None)
    return [e for e in entries
            if e["mountpoint"] != "/" and e["device"] != root_device]