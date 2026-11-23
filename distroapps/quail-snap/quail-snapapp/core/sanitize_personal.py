import hashlib
import os
import re
import stat
import subprocess

from core import home

ACCOUNT_FILES = ("etc/passwd", "etc/shadow", "etc/group", "etc/gshadow")
ACCOUNT_BACKUP_FILES = ("etc/passwd-", "etc/shadow-", "etc/group-", "etc/gshadow-")
SUDOERS_FILE = "etc/sudoers"
SUDOERS_D_DIR = "etc/sudoers.d"
SUBID_FILES = ("etc/subuid", "etc/subgid")
NETWORK_CREDENTIAL_DIRS = (
    "etc/NetworkManager/system-connections",
    "etc/wireguard",
    "etc/wpa_supplicant",
)
IDENTITY_FILES = (
    "etc/hostname", "etc/hosts", "etc/machine-id", "etc/machine-info",
    "var/lib/dbus/machine-id", "etc/resolv.conf",
)
FSTAB_FILE = "etc/fstab"
CRYPTTAB_FILE = "etc/crypttab"
SSH_DIR = "etc/ssh"
SSH_HOST_KEY_PREFIX = "ssh_host_"
LOG_DIR = "var/log"
FSCK_MARKERS = ("var/log/fsck/checkroot", "var/log/fsck/checkfs")
GRUB_GENERATED_FILES = ("boot/grub/grub.cfg", "boot/grub/grub.cfg.old",
                        "boot/grub/menu.lst")
ROOT_HOME = "root"
HOME_PREFIX = "/home/"

CACHE_DIR = "var/cache"
NM_STATE_DIR = "var/lib/NetworkManager"
NM_REMOVED_FILES = ("seen-bssids", "timestamps", "NetworkManager.state", "secret_key",
                    "NetworkManager-intern.conf")
NM_LEASE_PREFIX = "internal-"
NM_LEASE_SUFFIX = ".lease"
DHCP_STATE_DIR = "var/lib/dhcp"
APT_LISTS_DIR = "var/lib/apt/lists"
APT_LISTS_KEEP = ("partial", "auxfiles", "lock")

LIVE_USER = home.LIVE_USER
LIVE_HOME = "/home/" + LIVE_USER
DESKTOP_LAUNCHER_FILES = ("bcalcos_welcome.desktop", "bcalcos-installer.desktop")
KEYRING_DIR_REL = os.path.join(".local", "share", "keyrings")
# GVFS metadata is captured in full, but gvfsd-metadata may rotate it between
# capture and preservation verification on a live desktop; exclude only this
# exact directory from the exact manifest comparison.
GVFS_METADATA_DIR_REL = os.path.join(".local", "share", "gvfs-metadata")


class SanitizePersonalError(Exception):
    pass


def _rootfs(workspace):
    return os.path.join(workspace.path, "staging", "rootfs")


def _validate_rootfs(workspace):
    rootfs = _rootfs(workspace)
    if os.path.islink(rootfs) or not os.path.isdir(rootfs):
        raise SanitizePersonalError("staged rootfs missing or not a directory: " + rootfs)
    if not workspace.contains(rootfs):
        raise SanitizePersonalError("rootfs outside workspace: " + rootfs)
    return rootfs


def _validate_parent_dirs(rootfs, abs_path):
    cur = os.path.realpath(rootfs)
    for comp in abs_path.strip("/").split("/")[:-1]:
        cur = os.path.join(cur, comp)
        if os.path.lexists(cur) and (os.path.islink(cur) or not os.path.isdir(cur)):
            raise SanitizePersonalError("existing path component is not a directory: %s "
                                        "(for %r)" % (cur, abs_path))


def _safe_path(rootfs, workspace, abs_path, allow_symlink_leaf=False):
    if "\x00" in abs_path:
        raise SanitizePersonalError("NUL byte in path: %r" % abs_path)
    if not abs_path.startswith("/") or abs_path == "/":
        raise SanitizePersonalError("invalid path (must be absolute, not root): %r"
                                    % abs_path)
    if "//" in abs_path or abs_path.endswith("/"):
        raise SanitizePersonalError("empty path component in: %r" % abs_path)
    comps = abs_path.strip("/").split("/")
    for comp in comps:
        if not comp or comp in (".", ".."):
            raise SanitizePersonalError("unsafe path component in: %r" % abs_path)
    root_real = os.path.realpath(rootfs)
    cur = root_real
    for comp in comps[:-1]:
        cur = os.path.join(cur, comp)
        if os.path.islink(cur):
            raise SanitizePersonalError("symlink in path chain, refusing: %s (for %r)"
                                        % (cur, abs_path))
    target = os.path.join(rootfs, *comps)
    if os.path.lexists(target) and stat.S_ISLNK(os.lstat(target).st_mode) \
            and not allow_symlink_leaf:
        raise SanitizePersonalError("path is a symbolic link, refusing: %r" % abs_path)
    anc = target
    while os.path.islink(anc):
        anc = os.path.dirname(anc)
    anc_real = os.path.realpath(anc)
    if anc_real == root_real or not anc_real.startswith(root_real + os.sep):
        raise SanitizePersonalError("path escapes staged rootfs: %r" % abs_path)
    ws_real = os.path.realpath(workspace.path)
    if anc_real != ws_real and not anc_real.startswith(ws_real + os.sep):
        raise SanitizePersonalError("path outside workspace: " + target)
    return target


def _plan_target(rootfs, workspace, rel):
    path = _safe_path(rootfs, workspace, "/" + rel)
    _validate_parent_dirs(rootfs, "/" + rel)
    return path


def _validate_removal_path(rootfs, workspace, path):
    root_real = os.path.realpath(rootfs)
    rel = os.path.relpath(path, rootfs)
    if rel == ".." or rel.startswith(".." + os.sep):
        raise SanitizePersonalError("removal path escapes staged rootfs: " + path)
    cur = root_real
    for comp in rel.split(os.sep)[:-1]:
        cur = os.path.join(cur, comp)
        if os.path.islink(cur):
            raise SanitizePersonalError("symlink in removal path chain, refusing: " + cur)
    loc_real = os.path.realpath(os.path.dirname(path))
    if loc_real == root_real or not loc_real.startswith(root_real + os.sep):
        raise SanitizePersonalError("removal path escapes staged rootfs: " + path)
    if not path.startswith(rootfs + os.sep) and path != rootfs:
        raise SanitizePersonalError("removal path outside staged rootfs: " + path)
    if not workspace.contains(rootfs):
        raise SanitizePersonalError("rootfs outside workspace: " + rootfs)
    return path


def _walk_error(err):
    raise SanitizePersonalError("cannot traverse %s: %s"
                                % (err.filename, err.strerror))


def _plan_clear_dir(rootfs, workspace, rel):
    path = _plan_target(rootfs, workspace, rel)
    if not os.path.lexists(path):
        return None
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
        raise SanitizePersonalError("staged /%s is not a directory: %s" % (rel, path))
    plan = []
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False,
                                                onerror=_walk_error):
        for name in dirnames + filenames:
            plan.append(os.path.join(dirpath, name))
    return plan


def _plan_nm_state(rootfs, workspace):
    path = _plan_target(rootfs, workspace, NM_STATE_DIR)
    if not os.path.lexists(path):
        return []
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
        raise SanitizePersonalError("staged /%s is not a directory: %s"
                                    % (NM_STATE_DIR, path))
    plan = []
    for name in sorted(os.listdir(path)):
        if name in NM_REMOVED_FILES or (name.startswith(NM_LEASE_PREFIX)
                                       and name.endswith(NM_LEASE_SUFFIX)):
            child = os.path.join(path, name)
            cst = os.lstat(child)
            if stat.S_ISDIR(cst.st_mode) and not stat.S_ISLNK(cst.st_mode):
                raise SanitizePersonalError("NM state entry is a directory, refusing: "
                                            + child)
            plan.append(child)
    return plan


def _plan_apt_lists(rootfs, workspace):
    path = _plan_target(rootfs, workspace, APT_LISTS_DIR)
    if not os.path.lexists(path):
        return None
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
        raise SanitizePersonalError("staged /%s is not a directory: %s"
                                    % (APT_LISTS_DIR, path))
    plan = []
    for name in sorted(os.listdir(path)):
        child = os.path.join(path, name)
        if name == "lock":
            continue
        if name in ("partial", "auxfiles"):
            cst = os.lstat(child)
            if not stat.S_ISDIR(cst.st_mode) or stat.S_ISLNK(cst.st_mode):
                raise SanitizePersonalError("staged /%s/%s is not a directory: %s"
                                            % (APT_LISTS_DIR, name, child))
            for sub in sorted(os.listdir(child)):
                plan.append(os.path.join(child, sub))
            continue
        plan.append(child)
    return plan


def _execute_plan(plan):
    for p in sorted(plan, key=lambda x: x.count(os.sep), reverse=True):
        if not os.path.lexists(p):
            continue
        st = os.lstat(p)
        try:
            if stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode):
                os.rmdir(p)
            else:
                os.remove(p)
        except OSError as exc:
            raise SanitizePersonalError("cannot remove %s: %s" % (p, exc))


def _assert_regular_or_absent(path, rel):
    if not os.path.lexists(path):
        return
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        raise SanitizePersonalError("staged /%s is not a regular file: %s" % (rel, path))


def _plan_storage_config(rootfs, workspace):
    fstab = _plan_target(rootfs, workspace, FSTAB_FILE)
    _assert_regular_or_absent(fstab, FSTAB_FILE)
    removals = []
    crypttab = _plan_target(rootfs, workspace, CRYPTTAB_FILE)
    if os.path.lexists(crypttab):
        _assert_regular_or_absent(crypttab, CRYPTTAB_FILE)
        removals.append(crypttab)
    return {"truncate": [fstab], "removals": removals}


def _truncate_file(path, rel):
    flags = os.O_WRONLY | os.O_TRUNC | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o644)
    except OSError as exc:
        raise SanitizePersonalError("cannot empty staged /%s: %s" % (rel, exc))
    os.close(fd)


def sanitize(workspace):
    rootfs = _validate_rootfs(workspace)
    plan = []
    cache_plan = _plan_clear_dir(rootfs, workspace, CACHE_DIR)
    if cache_plan:
        plan += cache_plan
    plan += _plan_nm_state(rootfs, workspace)
    dhcp_plan = _plan_clear_dir(rootfs, workspace, DHCP_STATE_DIR)
    if dhcp_plan:
        plan += dhcp_plan
    apt_plan = _plan_apt_lists(rootfs, workspace)
    if apt_plan:
        plan += apt_plan
    storage = _plan_storage_config(rootfs, workspace)
    plan += storage["removals"]
    for rel in ACCOUNT_BACKUP_FILES:
        path = _plan_target(rootfs, workspace, rel)
        if os.path.lexists(path):
            st = os.lstat(path)
            if not stat.S_ISREG(st.st_mode):
                raise SanitizePersonalError("staged /%s is not a regular file: %s"
                                            % (rel, path))
            plan.append(path)
    for p in plan:
        _validate_removal_path(rootfs, workspace, p)
    _execute_plan(plan)
    for path in storage["truncate"]:
        _truncate_file(path, FSTAB_FILE)
    return rootfs


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_manifest(root, capture_dev=None):
    out = {}

    # The root itself must be on the original capture filesystem.
    # A foreign-device starting directory was not traversed by rsync.
    # capture_dev=None is reserved for the staged-side manifest.
    if capture_dev is not None:
        try:
            root_st = os.lstat(root)
        except OSError as exc:
            raise SanitizePersonalError(
                "cannot stat %s: %s" % (root, exc))

        if root_st.st_dev != capture_dev:
            return out

    for dirpath, dirnames, filenames in os.walk(
            root, followlinks=False, onerror=_walk_error):

        dir_names = set(dirnames)
        prune_dirs = set()

        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            st = os.lstat(full)

            if stat.S_ISLNK(st.st_mode):
                out[rel] = ("symlink", os.readlink(full))

            elif stat.S_ISDIR(st.st_mode):
                out[rel] = (
                    "dir", st.st_uid, st.st_gid,
                    stat.S_IMODE(st.st_mode)
                )

                # Only actual directories are subject to the
                # capture-source device boundary.
                if (capture_dev is not None
                        and name in dir_names
                        and st.st_dev != capture_dev):
                    prune_dirs.add(name)

            else:
                # Verified with rsync 3.5.0: --one-file-system prevents
        # traversal into directories on a different device, but
        # does not prevent copying regular files on another device.
        # Revalidate this behavior if the rsync version changes.
                out[rel] = (
                    "file", sha256_file(full), st.st_uid, st.st_gid,
                    stat.S_IMODE(st.st_mode), st.st_mtime
                )

        # Record all entries before modifying the traversal list.
        if prune_dirs:
            dirnames[:] = [
                name for name in dirnames
                if name not in prune_dirs
            ]

    return out

def _mirror_file(host_root, rootfs, workspace, rel):
    host_path = os.path.join(host_root, rel)
    staged_path = _safe_path(rootfs, workspace, "/" + rel, allow_symlink_leaf=True)
    host_exists = os.path.lexists(host_path)
    staged_exists = os.path.lexists(staged_path)
    if host_exists and not staged_exists:
        raise SanitizePersonalError("staged /%s missing but present on host" % rel)
    if staged_exists and not host_exists:
        raise SanitizePersonalError("staged /%s present but absent on host" % rel)
    if not host_exists:
        return
    hst = os.lstat(host_path)
    sst = os.lstat(staged_path)
    if stat.S_ISLNK(hst.st_mode) or stat.S_ISLNK(sst.st_mode):
        if not (stat.S_ISLNK(hst.st_mode) and stat.S_ISLNK(sst.st_mode)):
            raise SanitizePersonalError("staged /%s type mismatch vs host (symlink)" % rel)
        if os.readlink(host_path) != os.readlink(staged_path):
            raise SanitizePersonalError("staged /%s symlink target differs from host" % rel)
        return
    if stat.S_ISDIR(hst.st_mode) or stat.S_ISDIR(sst.st_mode):
        raise SanitizePersonalError("staged /%s type mismatch vs host (directory)" % rel)
    if not stat.S_ISREG(hst.st_mode) or not stat.S_ISREG(sst.st_mode):
        raise SanitizePersonalError("staged /%s type mismatch vs host (non-regular)" % rel)
    if sha256_file(host_path) != sha256_file(staged_path):
        raise SanitizePersonalError("staged /%s content differs from host" % rel)
    if (hst.st_uid, hst.st_gid, stat.S_IMODE(hst.st_mode)) != \
            (sst.st_uid, sst.st_gid, stat.S_IMODE(sst.st_mode)):
        raise SanitizePersonalError("staged /%s metadata differs from host" % rel)


def _mirror_dir(host_root, rootfs, workspace, rel,
                capture_dev, prefix=None):
    host_path = os.path.join(host_root, rel)
    staged_path = _safe_path(rootfs, workspace, "/" + rel)
    host_exists = os.path.lexists(host_path)
    staged_exists = os.path.lexists(staged_path)

    if host_exists and not staged_exists:
        raise SanitizePersonalError(
            "staged /%s missing but present on host" % rel)

    if staged_exists and not host_exists:
        raise SanitizePersonalError(
            "staged /%s present but absent on host" % rel)

    if not host_exists:
        return

    if not (stat.S_ISDIR(os.lstat(host_path).st_mode)
            and stat.S_ISDIR(os.lstat(staged_path).st_mode)):
        raise SanitizePersonalError(
            "staged /%s is not a directory" % rel)

    # Host traversal follows the original capture-source boundary.
    hman = _tree_manifest(host_path, capture_dev=capture_dev)

    # Staged traversal is independent of the host filesystem device.
    sman = _tree_manifest(staged_path)

    if prefix is not None:
        hman = {
            k: v for k, v in hman.items()
            if os.path.basename(k).startswith(prefix)
        }
        sman = {
            k: v for k, v in sman.items()
            if os.path.basename(k).startswith(prefix)
        }

    if hman != sman:
        only_host = sorted(set(hman) - set(sman))
        only_staged = sorted(set(sman) - set(hman))
        differing = sorted(
            k for k in set(hman) & set(sman)
            if hman[k] != sman[k]
        )

        raise SanitizePersonalError(
            "staged /%s differs from host (missing=%s, extra=%s, differing=%s)"
            % (rel, only_host, only_staged, differing))

def _parse_passwd(path):
    accounts = {}
    with open(path, "r") as fh:
        for lineno, line in enumerate(fh, 1):
            fields = line.rstrip("\n").split(":")
            if len(fields) != 7 or not fields[0]:
                raise SanitizePersonalError("malformed staged /etc/passwd line %d "
                                            "(contents withheld)" % lineno)
            uid_text = fields[2]
            if not uid_text or not all(c in "0123456789" for c in uid_text):
                raise SanitizePersonalError("malformed uid in staged /etc/passwd line %d "
                                            "(contents withheld)" % lineno)
            gid_text = fields[3]
            if not gid_text or not all(c in "0123456789" for c in gid_text):
                raise SanitizePersonalError("malformed gid in staged /etc/passwd line %d "
                                            "(contents withheld)" % lineno)
            accounts[fields[0]] = (int(uid_text), int(gid_text), fields[5])
    return accounts


def _assert_removals_done(rootfs, workspace):
    for rel in (CACHE_DIR, DHCP_STATE_DIR):
        path = _safe_path(rootfs, workspace, "/" + rel)
        if os.path.lexists(path):
            if not stat.S_ISDIR(os.lstat(path).st_mode):
                raise SanitizePersonalError("staged /%s is not a directory" % rel)
            if os.listdir(path):
                raise SanitizePersonalError("staged /%s not emptied: %s"
                                            % (rel, sorted(os.listdir(path))))
    nm = _safe_path(rootfs, workspace, "/" + NM_STATE_DIR)
    if os.path.lexists(nm):
        for name in os.listdir(nm):
            if name in NM_REMOVED_FILES or (name.startswith(NM_LEASE_PREFIX)
                                           and name.endswith(NM_LEASE_SUFFIX)):
                raise SanitizePersonalError("NM state file not removed: " + name)
    apt = _safe_path(rootfs, workspace, "/" + APT_LISTS_DIR)
    if os.path.lexists(apt):
        names = set(os.listdir(apt))
        extra = names - set(APT_LISTS_KEEP)
        if extra:
            raise SanitizePersonalError("staged /%s has non-retained entries: %s"
                                        % (APT_LISTS_DIR, sorted(extra)))
        for keep_dir in ("partial", "auxfiles"):
            kp = os.path.join(apt, keep_dir)
            if os.path.isdir(kp) and not os.path.islink(kp) and os.listdir(kp):
                raise SanitizePersonalError("staged /%s/%s not emptied"
                                            % (APT_LISTS_DIR, keep_dir))


def _assert_storage_config(rootfs, workspace):
    fstab = _safe_path(rootfs, workspace, "/" + FSTAB_FILE)
    if not os.path.isfile(fstab):
        raise SanitizePersonalError("staged /%s missing or not a regular file"
                                    % FSTAB_FILE)
    if os.path.getsize(fstab) != 0:
        raise SanitizePersonalError("staged /%s is not empty" % FSTAB_FILE)
    crypttab = _safe_path(rootfs, workspace, "/" + CRYPTTAB_FILE,
                          allow_symlink_leaf=True)
    if os.path.lexists(crypttab):
        raise SanitizePersonalError("staged /%s still present after sanitize"
                                    % CRYPTTAB_FILE)


def _read_lines(path):
    try:
        with open(path, "r") as fh:
            return fh.read().splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise SanitizePersonalError("cannot read %s: %s" % (path, exc))


def _read_text(path):
    try:
        with open(path, "r") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise SanitizePersonalError("cannot read %s: %s" % (path, exc))


# Password verification uses the system mkpasswd (the same utility Part 1
# uses for yescrypt hash generation). core/preflight.py REQUIRED_BINARIES
# already guarantees it is installed before any build runs; verification
# still fails closed if the executable is somehow absent at this point.
MKPASSWD = "mkpasswd"
MKPASSWD_TIMEOUT = 60

# A complete yescrypt hash: $y$<params>$<salt>$<hash>
_YESSCRYPT_HASH_RE = re.compile(r"^\$y\$[^$\s]+\$[^$\s]+\$[^$\s]+$")


def _mkpasswd_env():
    # Copied environment with MKPASSWD_OPTIONS explicitly removed so that
    # inherited options cannot influence verification. The password is never
    # placed in the subprocess environment.
    env = dict(os.environ)
    env.pop("MKPASSWD_OPTIONS", None)
    return env


def _mkpasswd_recompute(salt_hash, password):
    """Recompute a password hash with the system mkpasswd and return it.

    The complete stored hash is supplied via --salt (mkpasswd passes a
    '$'-prefixed salt straight to crypt(3), so the full hash acts as the
    verification salt). The password is supplied on stdin via
    --password-fd=0 - never as a command-line argument and never in the
    environment. No shell is used; stdout (resulting hash) and stderr are
    captured separately.

    Fails closed (raises SanitizePersonalError) if mkpasswd is missing,
    times out, exits nonzero, or returns empty, malformed or non-yescrypt
    output. Error messages deliberately contain only the utility name and
    the exit status - never the hash, the password or the command line.
    """
    if "\n" in password or "\r" in password:
        raise SanitizePersonalError(
            "password cannot be supplied to mkpasswd safely (fail closed)")
    argv = [MKPASSWD, "--salt", salt_hash, "--password-fd=0"]
    try:
        proc = subprocess.run(argv, input=(password + "\n").encode("utf-8"),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=_mkpasswd_env(), timeout=MKPASSWD_TIMEOUT)
    except FileNotFoundError:
        raise SanitizePersonalError(
            "mkpasswd is not available for password verification (fail closed)")
    except subprocess.TimeoutExpired:
        raise SanitizePersonalError(
            "mkpasswd timed out during password verification (fail closed)")
    except OSError:
        raise SanitizePersonalError(
            "mkpasswd could not be executed for password verification (fail closed)")
    if proc.returncode != 0:
        raise SanitizePersonalError(
            "mkpasswd exited with status %d during password verification "
            "(fail closed)" % proc.returncode)
    try:
        out = proc.stdout.decode("ascii")
    except UnicodeDecodeError:
        raise SanitizePersonalError(
            "mkpasswd returned non-ASCII output during password verification "
            "(fail closed)")
    if out.endswith("\n"):
        out = out[:-1]
    if not out or "\n" in out or "\r" in out:
        raise SanitizePersonalError(
            "mkpasswd returned empty or malformed output during password "
            "verification (fail closed)")
    if not _YESSCRYPT_HASH_RE.match(out):
        raise SanitizePersonalError(
            "mkpasswd did not return a yescrypt hash during password "
            "verification (fail closed)")
    return out


def _password_matches(hash_, password):
    """Verify a stored yescrypt hash against a password using system mkpasswd.

    Returns True only when mkpasswd recomputes exactly the stored hash.
    A stored value that is not a well-formed yescrypt hash is rejected
    (False) without invoking mkpasswd. Any failure to perform the
    verification itself fails closed (raises SanitizePersonalError), so an
    unverified hash can never appear valid when the utility is unavailable.
    """
    if not isinstance(hash_, str) or not _YESSCRYPT_HASH_RE.match(hash_):
        return False
    # Reject NUL and other non-printable control characters explicitly before
    # the hash can reach the subprocess argument list: \\s in the regex above
    # does not cover them, and a NUL in an argv element would otherwise
    # surface as a raw ValueError from subprocess.
    if any(ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F for c in hash_):
        return False
    computed = _mkpasswd_recompute(hash_, password)
    return computed == hash_


def _xf_token(tok, old, new):
    neg = tok.startswith("!")
    t = tok[1:] if neg else tok
    if t == old:
        return ("!" + new) if neg else new
    if t == new:
        raise SanitizePersonalError("collision: %r already present in list" % new)
    if old in t:
        raise SanitizePersonalError("ambiguous token %r contains %r, refusing"
                                    % (tok, old))
    return tok


def _xf_member_list(entries, old, new):
    out = []
    for e in entries:
        e = e.strip()
        if "(" in e or '"' in e or "\\\\" in e:
            raise SanitizePersonalError("malformed list member %r, refusing" % e)
        out.append(_xf_token(e, old, new))
    return out


def xf_passwd_lines(lines, old, new, new_home):
    out = []
    for lineno, ln in enumerate(lines, 1):
        fields = ln.split(":")
        if len(fields) != 7 or not fields[0]:
            raise SanitizePersonalError("malformed passwd line %d (contents withheld)"
                                        % lineno)
        if fields[0] == new and fields[0] != old:
            raise SanitizePersonalError("account %r already exists in passwd" % new)
        if fields[0] == old:
            fields[0] = new
            fields[5] = new_home
        out.append(":".join(fields))
    return out


def xf_shadow_lines(lines, old, new, password_hash=None, mask_hash=False):
    out = []
    for lineno, ln in enumerate(lines, 1):
        fields = ln.split(":")
        if len(fields) != 9 or not fields[0]:
            raise SanitizePersonalError("malformed shadow line %d (contents withheld)"
                                        % lineno)
        if fields[0] == new and fields[0] != old:
            raise SanitizePersonalError("account %r already exists in shadow" % new)
        if fields[0] == old:
            fields[0] = new
            if password_hash is not None:
                fields[1] = password_hash
            elif mask_hash:
                fields[1] = "<HASH>"
        out.append(":".join(fields))
    return out


def xf_group_lines(lines, old, new, rename_gid=None):
    out = []
    for lineno, ln in enumerate(lines, 1):
        fields = ln.split(":")
        if len(fields) not in (3, 4) or not fields[0]:
            raise SanitizePersonalError("malformed group line %d (contents withheld)"
                                        % lineno)
        if fields[0] == new and fields[0] != old:
            raise SanitizePersonalError("group %r already exists" % new)
        if rename_gid is not None and fields[0] == old and fields[2] == str(rename_gid):
            fields[0] = new
        if len(fields) == 4 and fields[3]:
            fields[3] = ",".join(_xf_member_list(fields[3].split(","), old, new))
        out.append(":".join(fields))
    return out


def xf_gshadow_lines(lines, old, new, rename_group=False):
    out = []
    for lineno, ln in enumerate(lines, 1):
        fields = ln.split(":")
        if len(fields) != 4 or not fields[0]:
            raise SanitizePersonalError("malformed gshadow line %d (contents withheld)"
                                        % lineno)
        if fields[0] == new and fields[0] != old:
            raise SanitizePersonalError("group %r already exists in gshadow" % new)
        if rename_group and fields[0] == old:
            fields[0] = new
        for idx in (2, 3):
            if fields[idx]:
                fields[idx] = ",".join(_xf_member_list(fields[idx].split(","), old, new))
        out.append(":".join(fields))
    return out


def xf_subid_lines(lines, old, new):
    out = []
    for lineno, ln in enumerate(lines, 1):
        fields = ln.split(":")
        if len(fields) != 3 or not fields[0] or not fields[1].isdigit() \
                or not fields[2].isdigit():
            raise SanitizePersonalError("malformed subid line %d (contents withheld)"
                                        % lineno)
        if fields[0] == old:
            fields[0] = new
        out.append(":".join(fields))
    return out


def _split_comment(ln):
    for i, ch in enumerate(ln):
        if ch == "#" and not (i + 1 < len(ln) and ln[i + 1].isdigit()):
            return ln[:i], ln[i:]
    return ln, ""


def _split_leading_list(text):
    tokens = text.split()
    if not tokens:
        raise SanitizePersonalError("cannot parse sudoers list: %r" % text)
    i = 0
    while i < len(tokens) - 1 and (tokens[i].endswith(",") or tokens[i + 1].startswith(",")):
        i += 1
    return " ".join(tokens[:i + 1]), " ".join(tokens[i + 1:])


_ALIAS_DEF_KEYWORDS = ("User_Alias", "Runas_Alias")
_ALIAS_KEEP_KEYWORDS = ("Cmnd_Alias", "Host_Alias")


_SUDOERS_TAG_COLON_RE = re.compile(
    r"(?:NOPASSWD|PASSWD|NOEXEC|EXEC|SETENV|NOSETENV|LOG_INPUT|NOLOG_INPUT|"
    r"LOG_OUTPUT|NOLOG_OUTPUT|MAIL|NOMAIL|FOLLOW|NOFOLLOW|INTERCEPT|NOINTERCEPT):")


def _xf_runas_spec(rest, old, new):
    if "=" not in rest:
        return rest
    if rest.count("=") > 1:
        raise SanitizePersonalError("sudoers spec with multiple '=', refusing: %r" % rest)
    head, _, rhs = rest.partition("=")
    open_idx = rhs.find("(")
    if open_idx == -1 or rhs[:open_idx].strip() != "":
        return rest
    close = rhs.find(")", open_idx + 1)
    if close == -1 or "(" in rhs[open_idx + 1:close] or ")" in rhs[close + 1:]:
        raise SanitizePersonalError("unsupported sudoers runas spec, refusing: %r" % rhs)
    tail = rhs[close + 1:]
    if ":" in _SUDOERS_TAG_COLON_RE.sub("", tail):
        raise SanitizePersonalError("sudoers multiple command specs, refusing: %r" % rhs)
    gap = rhs[:open_idx]
    inner = rhs[open_idx + 1:close]
    if ":" in inner:
        user_part, _, group_part = inner.partition(":")
        if ":" in group_part:
            raise SanitizePersonalError("malformed runas group list, refusing: %r" % inner)
    else:
        user_part, group_part = inner, None
    user_tokens = [t for t in user_part.split(",") if t.strip()]
    kept_users = _xf_member_list(user_tokens, old, new)
    if group_part is None:
        inner_out = ", ".join(kept_users)
    else:
        group_tokens = [t.strip() for t in group_part.split(",") if t.strip()]
        for g in group_tokens:
            if "(" in g or '"' in g or "\\\\" in g:
                raise SanitizePersonalError("malformed runas group member %r, refusing" % g)
        inner_out = ", ".join(kept_users) + ":" + ", ".join(group_tokens)
    return head + "=" + gap + "(" + inner_out + ")" + tail


def xf_sudoers_line(ln, old, new):
    s = ln.strip()
    if not s or (s.startswith("#") and not s[1:2].isdigit()):
        return ln
    if s.endswith("\\\\"):
        raise SanitizePersonalError("sudoers backslash-continued line, refusing: %r" % ln)
    body, comment = _split_comment(ln)
    stripped = body.strip()
    for k in _ALIAS_KEEP_KEYWORDS:
        if stripped.startswith(k):
            return ln
    if stripped.startswith("Defaults"):
        after = stripped[len("Defaults"):]
        if after[:1] in (":", ">"):
            list_str, tail = _split_leading_list(after[1:].strip())
            entries = [t for t in list_str.split(",") if t.strip()]
            kept = _xf_member_list(entries, old, new)
            out = "Defaults" + after[:1] + ", ".join(kept)
            if tail:
                out += " " + tail
            if comment:
                out += " " + comment
            return out
        return ln
    for k in _ALIAS_DEF_KEYWORDS:
        if stripped.startswith(k):
            rest = stripped[len(k):].strip()
            new_defs = []
            for d in rest.split(":"):
                if "=" not in d:
                    raise SanitizePersonalError("malformed %s definition, refusing: %r"
                                                % (k, ln))
                name, _, members_str = d.partition("=")
                entries = [t for t in members_str.split(",") if t.strip()]
                kept = _xf_member_list(entries, old, new)
                new_defs.append("%s = %s" % (name.strip(), ", ".join(kept)))
            out = k + " " + " : ".join(new_defs)
            if comment:
                out += " " + comment
            return out
    list_str, rest = _split_leading_list(stripped)
    entries = [t for t in list_str.split(",") if t.strip()]
    kept = _xf_member_list(entries, old, new)
    out = ", ".join(kept)
    if rest:
        out += " " + _xf_runas_spec(rest, old, new)
    if comment:
        out += " " + comment
    return out


def xf_sudoers_text(text, old, new):
    return "\n".join(xf_sudoers_line(ln, old, new) for ln in text.split("\n"))


def _is_launcher_delta_name(name):
    if name in DESKTOP_LAUNCHER_FILES:
        return True
    prefix = "quail-snap-"
    if name.startswith(prefix):
        rest = name[len(prefix):]
        for lf in DESKTOP_LAUNCHER_FILES:
            if rest == lf:
                return True
            if rest.startswith(lf + "-") and rest[len(lf) + 1:].isdigit():
                return True
    return False


def _filter_home_manifest(man, drop_keyrings=False, drop_launchers=False,
                          drop_gvfs_metadata=False):
    out = {}
    for k, v in man.items():
        parts = k.split(os.sep)
        if drop_launchers and len(parts) == 2 and parts[0] == "Desktop" \
                and _is_launcher_delta_name(parts[1]):
            continue
        if drop_keyrings and k.startswith(KEYRING_DIR_REL + os.sep) \
                and k.endswith(".keyring"):
            continue
        if drop_gvfs_metadata and (k == GVFS_METADATA_DIR_REL
                                   or k.startswith(GVFS_METADATA_DIR_REL + os.sep)):
            continue
        out[k] = v
    return out


def _verify_home_contents(host_root, rootfs, workspace, capture_dev, host_home,
                          live_home):
    host_path = os.path.join(host_root, host_home.lstrip("/"))
    staged_path = _safe_path(rootfs, workspace, live_home)
    if not os.path.isdir(host_path):
        return
    hst = os.lstat(host_path)
    sst = os.lstat(staged_path)
    if (hst.st_uid, hst.st_gid, stat.S_IMODE(hst.st_mode)) != \
            (sst.st_uid, sst.st_gid, stat.S_IMODE(sst.st_mode)):
        raise SanitizePersonalError(
            "staged %s root uid/gid/mode differs from host %s" % (live_home, host_home))
    hman_raw = _tree_manifest(host_path, capture_dev=os.lstat(host_path).st_dev)
    # A Desktop directory created by Quail Snap legitimately changes the home
    # root's mtime; otherwise the preserved timestamp must match exactly.
    if "Desktop" in hman_raw and hst.st_mtime != sst.st_mtime:
        raise SanitizePersonalError(
            "staged %s root mtime differs from host %s" % (live_home, host_home))
    hman = _filter_home_manifest(hman_raw, drop_keyrings=True, drop_launchers=True,
                                 drop_gvfs_metadata=True)
    sman = _filter_home_manifest(_tree_manifest(staged_path), drop_launchers=True,
                                 drop_gvfs_metadata=True)
    if "Desktop" not in hman:
        sman.pop("Desktop", None)
    if hman != sman:
        only_host = sorted(set(hman) - set(sman))
        only_staged = sorted(set(sman) - set(hman))
        differing = sorted(k for k in set(hman) & set(sman) if hman[k] != sman[k])
        raise SanitizePersonalError(
            "staged %s differs from host %s (missing=%s, extra=%s, differing=%s)"
            % (live_home, host_home, only_host, only_staged, differing))


def _mask_live_hash(lines, name):
    out = []
    for ln in lines:
        f = ln.split(":")
        if f and f[0] == name and len(f) == 9:
            f[1] = "<HASH>"
        out.append(":".join(f))
    return out


def _expected_account_lines(host_root, rel, creator, rename_gid, rename_group):
    lines = _read_lines(os.path.join(host_root, rel))
    if rel == "etc/passwd":
        return xf_passwd_lines(lines, creator, LIVE_USER, LIVE_HOME)
    if rel == "etc/shadow":
        return xf_shadow_lines(lines, creator, LIVE_USER)
    if rel == "etc/group":
        return xf_group_lines(lines, creator, LIVE_USER, rename_gid=rename_gid)
    if rel == "etc/gshadow":
        return xf_gshadow_lines(lines, creator, LIVE_USER, rename_group=rename_group)
    raise SanitizePersonalError("unexpected account file: " + rel)


def _verify_account_transform(host_root, rootfs, workspace, creator):
    host_passwd = os.path.join(host_root, "etc", "passwd")
    if not os.path.isfile(host_passwd):
        raise SanitizePersonalError("host /etc/passwd missing or not a regular file: "
                                    + host_passwd)
    host_accounts = _parse_passwd(host_passwd)
    if creator not in host_accounts:
        raise SanitizePersonalError("creator %r missing from host /etc/passwd" % creator)
    uid, gid, host_home = host_accounts[creator]

    gid_name = None
    for ln in _read_lines(os.path.join(host_root, "etc", "group")):
        f = ln.split(":")
        if len(f) >= 3 and f[2] == str(gid):
            gid_name = f[0]
            break
    rename_group = gid_name == creator
    rename_gid = gid if rename_group else None

    for rel in ACCOUNT_FILES:
        host_path = os.path.join(host_root, rel)
        staged_path = _safe_path(rootfs, workspace, "/" + rel)
        if not os.path.isfile(host_path):
            if os.path.lexists(staged_path):
                raise SanitizePersonalError("staged /%s present but absent on host" % rel)
            continue
        if not os.path.isfile(staged_path):
            raise SanitizePersonalError("staged /%s missing (host has it)" % rel)
        expected = _expected_account_lines(host_root, rel, creator, rename_gid,
                                           rename_group)
        actual = _read_lines(staged_path)
        if rel == "etc/shadow":
            expected = _mask_live_hash(expected, LIVE_USER)
            actual = _mask_live_hash(actual, LIVE_USER)
        if actual != expected:
            raise SanitizePersonalError(
                "staged /%s does not match the expected normalized content" % rel)

    staged_accounts = _parse_passwd(_safe_path(rootfs, workspace, "/etc/passwd"))
    if LIVE_USER not in staged_accounts:
        raise SanitizePersonalError("staged live account %r missing" % LIVE_USER)
    s_uid, s_gid, s_home = staged_accounts[LIVE_USER]
    if (s_uid, s_gid) != (uid, gid):
        raise SanitizePersonalError("staged %s has uid/gid %s/%s, expected %s/%s"
                                    % (LIVE_USER, s_uid, s_gid, uid, gid))
    if s_home != LIVE_HOME:
        raise SanitizePersonalError("staged %s home is %r, expected %r"
                                    % (LIVE_USER, s_home, LIVE_HOME))
    if creator != LIVE_USER and creator in staged_accounts:
        raise SanitizePersonalError("stale creator account %r still in staged /etc/passwd"
                                    % creator)

    staged_hash = None
    for ln in _read_lines(_safe_path(rootfs, workspace, "/etc/shadow")):
        f = ln.split(":")
        if f and f[0] == LIVE_USER:
            staged_hash = f[1]
    if staged_hash is None:
        raise SanitizePersonalError("staged /etc/shadow has no %r entry" % LIVE_USER)
    if not _password_matches(staged_hash, home.LIVE_USER_PASSWORD):
        raise SanitizePersonalError("staged %s password hash does not verify as the "
                                    "published live password" % LIVE_USER)

    for rel in (SUDOERS_FILE,) + SUBID_FILES:
        host_path = os.path.join(host_root, rel)
        staged_path = _safe_path(rootfs, workspace, "/" + rel)
        if not os.path.lexists(host_path):
            if os.path.lexists(staged_path):
                raise SanitizePersonalError("staged /%s present but absent on host" % rel)
            continue
        if not os.path.isfile(host_path) or not os.path.isfile(staged_path):
            raise SanitizePersonalError("staged /%s missing or not a regular file" % rel)
        if rel == SUDOERS_FILE:
            expected = xf_sudoers_text(_read_text(host_path), creator, LIVE_USER)
            if _read_text(staged_path) != expected:
                raise SanitizePersonalError(
                    "staged /etc/sudoers does not match the expected normalized content")
        else:
            expected = xf_subid_lines(_read_lines(host_path), creator, LIVE_USER)
            if _read_lines(staged_path) != expected:
                raise SanitizePersonalError(
                    "staged /%s does not match the expected normalized content" % rel)

    for rel in ACCOUNT_BACKUP_FILES:
        p = _safe_path(rootfs, workspace, "/" + rel, allow_symlink_leaf=True)
        if os.path.lexists(p):
            raise SanitizePersonalError("staged /%s still present; backups must be removed"
                                        % rel)

    host_d = os.path.join(host_root, SUDOERS_D_DIR)
    staged_d = _safe_path(rootfs, workspace, "/" + SUDOERS_D_DIR)
    if os.path.lexists(host_d):
        if os.path.islink(host_d) or not os.path.isdir(host_d):
            raise SanitizePersonalError("host /%s is not a real directory" % SUDOERS_D_DIR)
        if os.path.islink(staged_d) or not os.path.isdir(staged_d):
            raise SanitizePersonalError("staged /%s missing or not a real directory"
                                        % SUDOERS_D_DIR)
        expected = {}
        for name in sorted(os.listdir(host_d)):
            hp = os.path.join(host_d, name)
            if os.path.islink(hp) or not os.path.isfile(hp):
                raise SanitizePersonalError("host /%s/%s is not a regular file"
                                            % (SUDOERS_D_DIR, name))
            mapped = LIVE_USER if name == creator else name
            expected[mapped] = xf_sudoers_text(_read_text(hp), creator, LIVE_USER)
        actual = {}
        for name in sorted(os.listdir(staged_d)):
            sdp = os.path.join(staged_d, name)
            if os.path.islink(sdp) or not os.path.isfile(sdp):
                raise SanitizePersonalError("staged /%s/%s is not a regular file"
                                            % (SUDOERS_D_DIR, name))
            actual[name] = _read_text(sdp)
        if actual != expected:
            raise SanitizePersonalError(
                "staged /%s does not match expected normalized contents "
                "(missing=%s, extra=%s, differing=%s)"
                % (SUDOERS_D_DIR, sorted(set(expected) - set(actual)),
                   sorted(set(actual) - set(expected)),
                   sorted(k for k in set(actual) & set(expected)
                          if actual[k] != expected[k])))
    elif os.path.lexists(staged_d):
        raise SanitizePersonalError("staged /%s present but absent on host"
                                    % SUDOERS_D_DIR)

    return {"uid": uid, "gid": gid, "host_home": host_home}


def verify_preservation(workspace, creator, host_root="/"):
    rootfs = _validate_rootfs(workspace)

    # This must remain aligned with capture_personal.capture_root(),
    # whose production source defaults to "/".
    # Sample the original capture-source device exactly once.
    try:
        capture_source_st = os.lstat(host_root)
    except OSError as exc:
        raise SanitizePersonalError(
            "cannot stat capture source %s: %s" % (host_root, exc))

    if (stat.S_ISLNK(capture_source_st.st_mode)
            or not stat.S_ISDIR(capture_source_st.st_mode)):
        raise SanitizePersonalError(
            "capture source is not a directory: %s" % host_root)

    capture_dev = capture_source_st.st_dev

    for rel in IDENTITY_FILES + FSCK_MARKERS + GRUB_GENERATED_FILES:
        _mirror_file(host_root, rootfs, workspace, rel)

    acct = _verify_account_transform(host_root, rootfs, workspace, creator)

    for rel in NETWORK_CREDENTIAL_DIRS:
        _mirror_dir(host_root, rootfs, workspace, rel, capture_dev)

    _mirror_dir(host_root, rootfs, workspace,
                SSH_DIR, capture_dev, prefix=SSH_HOST_KEY_PREFIX)

    _mirror_dir(host_root, rootfs, workspace, ROOT_HOME, capture_dev)

    host_log = os.path.join(host_root, LOG_DIR)
    log_dir = _safe_path(rootfs, workspace, "/" + LOG_DIR)

    if os.path.lexists(host_log):
        if (not os.path.lexists(log_dir)
                or not stat.S_ISDIR(os.lstat(log_dir).st_mode)):
            raise SanitizePersonalError(
                "staged /%s missing or not a directory "
                "(host has it)" % LOG_DIR)

    elif os.path.lexists(log_dir):
        raise SanitizePersonalError(
            "staged /%s present but absent on host" % LOG_DIR)

    passwd_path = _safe_path(rootfs, workspace, "/etc/passwd")
    accounts = _parse_passwd(passwd_path)

    if LIVE_USER not in accounts:
        raise SanitizePersonalError(
            "live account %r missing from staged /etc/passwd" % LIVE_USER)

    live_home_path = _safe_path(rootfs, workspace, LIVE_HOME)
    if (not os.path.lexists(live_home_path)
            or not stat.S_ISDIR(os.lstat(live_home_path).st_mode)):
        raise SanitizePersonalError(
            "normalized home missing or not a directory: %s" % LIVE_HOME)

    if acct["host_home"]:
        if acct["host_home"] != LIVE_HOME:
            old_home_path = _safe_path(rootfs, workspace, acct["host_home"],
                                       allow_symlink_leaf=True)
            if os.path.lexists(old_home_path):
                raise SanitizePersonalError(
                    "original home still present after normalization: %s"
                    % acct["host_home"])
        _verify_home_contents(host_root, rootfs, workspace, capture_dev,
                              acct["host_home"], LIVE_HOME)

    _assert_removals_done(rootfs, workspace)
    _assert_storage_config(rootfs, workspace)
    return True