import os
import stat
import subprocess

from core import home

NETWORK_CREDENTIAL_DIRS = (
    "etc/NetworkManager/system-connections",
    "etc/wireguard",
    "etc/wpa_supplicant",
)
SSH_HOST_KEY_GLOB = "ssh_host_"
LOG_DIRS = ("var/log",)
CACHE_DIRS = ("var/cache",)
ROOT_KEEP_FILES = (
    "root/.bashrc",
    "root/.profile",
)
PROTECTED_ACCOUNTS = ("root", home.LIVE_USER)
MIN_USER_UID = 1000
# BC-ratified protected system prefixes (component-wise matching):
PROTECTED_SYSTEM_DIRS = (
    "bin", "boot", "dev", "etc", "lib", "lib32", "lib64", "libx32", "media",
    "mnt", "opt", "proc", "root", "run", "sbin", "sys", "tmp", "usr", "var",
)
PROTECTED_CONTAINER_DIRS = ("home", "srv")
ACCOUNT_FILES = ("etc/passwd", "etc/shadow", "etc/group", "etc/gshadow")
HOSTNAME_VALUE = "bcalcos"
HOSTS_SKELETON_LINES = (
    "127.0.0.1 localhost",
    "127.0.1.1 " + HOSTNAME_VALUE,
)
HOSTNAME_TARGETS = ("etc/hostname", "etc/hosts", "etc/machine-info")
ACCOUNT_BACKUP_FILES = ("etc/passwd-", "etc/shadow-", "etc/group-", "etc/gshadow-")
SUBID_FILES = ("etc/subuid", "etc/subgid")
NM_STATE_DIR = "var/lib/NetworkManager"
NM_REMOVED_FILES = ("seen-bssids", "timestamps", "NetworkManager.state",
                    "secret_key", "NetworkManager-intern.conf")
NM_LEASE_PREFIX = "internal-"
DHCP_STATE_DIR = "var/lib/dhcp"
APT_LISTS_DIR = "var/lib/apt/lists"
APT_LISTS_KEEP = ("partial", "auxfiles", "lock")
GRUB_GENERATED_FILES = ("boot/grub/grub.cfg", "boot/grub/grub.cfg.old",
                        "boot/grub/menu.lst")


class SanitizeError(Exception):
    pass


def _rootfs(workspace):
    return os.path.join(workspace.path, "staging", "rootfs")


def _validate_rootfs(workspace):
    rootfs = _rootfs(workspace)
    staging = os.path.dirname(rootfs)
    for p in (staging, rootfs):
        if os.path.islink(p):
            raise SanitizeError("symlinked staging path, refusing: " + p)
    if not workspace.contains(rootfs):
        raise SanitizeError("rootfs outside workspace: " + rootfs)
    if not os.path.isdir(rootfs):
        raise SanitizeError("staged rootfs missing or not a directory: " + rootfs)
    return rootfs


def _safe_path(rootfs, workspace, abs_path, allow_symlink_leaf=False):
    if "\x00" in abs_path:
        raise SanitizeError("NUL byte in path: %r" % abs_path)
    if not abs_path.startswith("/") or abs_path == "/":
        raise SanitizeError("invalid path (must be absolute, not root): %r" % abs_path)
    if "//" in abs_path or abs_path.endswith("/"):
        raise SanitizeError("empty path component in: %r" % abs_path)
    comps = abs_path.strip("/").split("/")
    for comp in comps:
        if not comp or comp in (".", ".."):
            raise SanitizeError("unsafe path component in: %r" % abs_path)
    target = os.path.join(rootfs, *comps)
    root_real = os.path.realpath(rootfs)
    cur = root_real
    for comp in comps[:-1]:
        cur = os.path.join(cur, comp)
        if os.path.islink(cur):
            raise SanitizeError("symlink in path chain, refusing: %s (for %r)"
                                % (cur, abs_path))
    if os.path.islink(target) and not allow_symlink_leaf:
        raise SanitizeError("path is a symbolic link, refusing: %r" % abs_path)
    anc = target
    while os.path.islink(anc):
        anc = os.path.dirname(anc)
    anc_real = os.path.realpath(anc)
    if anc_real == root_real or not anc_real.startswith(root_real + os.sep):
        raise SanitizeError("path escapes staged rootfs: %r" % abs_path)
    ws_real = os.path.realpath(workspace.path)
    if anc_real != ws_real and not anc_real.startswith(ws_real + os.sep):
        raise SanitizeError("path outside workspace: " + target)
    return target


def _validate_parent_dirs(rootfs, abs_path):
    cur = os.path.realpath(rootfs)
    for comp in abs_path.strip("/").split("/")[:-1]:
        cur = os.path.join(cur, comp)
        if os.path.lexists(cur) and (os.path.islink(cur) or not os.path.isdir(cur)):
            raise SanitizeError("existing path component is not a directory: %s (for %r)"
                                % (cur, abs_path))


def _account_path(rootfs, workspace, rel):
    path = _safe_path(rootfs, workspace, "/" + rel)
    _validate_parent_dirs(rootfs, "/" + rel)
    if not os.path.isfile(path):
        raise SanitizeError("staged /%s missing or not a regular file: %s" % (rel, path))
    return path


def _read_lines(path):
    try:
        with open(path, "r") as fh:
            return fh.read().splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise SanitizeError("cannot read staged account file %s: %s" % (path, exc))


def _write_lines(path, lines, what):
    try:
        with open(path, "w") as fh:
            fh.writelines(lines)
    except (OSError, UnicodeEncodeError) as exc:
        raise SanitizeError("cannot write staged %s %s: %s" % (what, path, exc))


def _valid_uid(text):
    return bool(text) and all(c in "0123456789" for c in text)


def _parse_uid(text, line):
    if not _valid_uid(text):
        raise SanitizeError("malformed uid in staged /etc/passwd line: %r" % line)
    try:
        return int(text)
    except ValueError as exc:
        raise SanitizeError("uid conversion failed in staged /etc/passwd line %r: %s"
                            % (line, exc))


def _validate_home_field(home_dir, uid):
    if not home_dir:
        raise SanitizeError("empty home field in staged /etc/passwd")
    if "\x00" in home_dir or not home_dir.startswith("/"):
        raise SanitizeError("invalid home path in staged /etc/passwd: %r" % home_dir)
    if home_dir == "/":
        if uid >= MIN_USER_UID:
            raise SanitizeError("home path / is not allowed for a user account "
                                "in staged /etc/passwd: %r" % home_dir)
    elif "//" in home_dir or home_dir.endswith("/"):
        raise SanitizeError("empty path component in home path in staged /etc/passwd: %r"
                            % home_dir)
    for comp in home_dir.strip("/").split("/"):
        if comp in (".", ".."):
            raise SanitizeError("traversal component in home path in staged /etc/passwd: %r"
                                % home_dir)


def _parse_passwd(lines):
    accounts = []
    seen = set()
    for line in lines:
        fields = line.split(":")
        if len(fields) != 7 or not fields[0]:
            raise SanitizeError("malformed staged /etc/passwd line: %r" % line)
        name = fields[0]
        if name in seen:
            raise SanitizeError("duplicate username in staged /etc/passwd: %r" % name)
        seen.add(name)
        if "/" in name or "\x00" in name or name in (".", ".."):
            raise SanitizeError("unsafe username in staged /etc/passwd line: %r" % line)
        uid = _parse_uid(fields[2], line)
        _validate_home_field(fields[5], uid)
        accounts.append((name, uid, fields[5]))
    return accounts


def _parse_shadow(shadow_path):
    for line in _read_lines(shadow_path):
        fields = line.split(":")
        if len(fields) != 9 or not fields[0]:
            raise SanitizeError("malformed staged /etc/shadow line: %r" % line)


def _parse_grouplike(path, what):
    for line in _read_lines(path):
        fields = line.split(":")
        if len(fields) not in (3, 4) or not fields[0]:
            raise SanitizeError("malformed staged %s line: %r" % (what, line))


def _home_in_protected_location(home_dir):
    comps = home_dir.strip("/").split("/")
    first = comps[0]
    if first in PROTECTED_SYSTEM_DIRS or (first.startswith("lib") and first[3:].isdigit()):
        return True
    return first in PROTECTED_CONTAINER_DIRS and len(comps) == 1


def _check_home_allowed(home_dir):
    comps = home_dir.strip("/").split("/")
    first = comps[0]
    if first in PROTECTED_SYSTEM_DIRS or (first.startswith("lib") and first[3:].isdigit()):
        raise SanitizeError("home path in protected system directory: %r" % home_dir)
    if first in PROTECTED_CONTAINER_DIRS and len(comps) == 1:
        raise SanitizeError("home path is a protected container directory: %r" % home_dir)


def _home_target(rootfs, workspace, home_dir, preserved_homes):
    _check_home_allowed(home_dir)
    target = _safe_path(rootfs, workspace, home_dir)
    _validate_parent_dirs(rootfs, home_dir)
    if os.path.lexists(target):
        target_real = os.path.realpath(target)
        for ph in preserved_homes:
            if (target_real == ph or target_real.startswith(ph + os.sep)
                    or ph.startswith(target_real + os.sep)):
                raise SanitizeError("home path overlaps a preserved account's home: %r" % home_dir)
    return target


def _validate_fstab_crypttab(rootfs, workspace):
    paths = {}
    for rel in ("etc/fstab", "etc/crypttab"):
        path = _safe_path(rootfs, workspace, "/" + rel)
        _validate_parent_dirs(rootfs, "/" + rel)
        if os.path.lexists(path) and not os.path.isfile(path):
            raise SanitizeError("staged /%s is not a regular file: %s" % (rel, path))
        paths[rel] = path
    return paths


def _validate_root_home(rootfs, workspace, accounts):
    root_acct = next((a for a in accounts if a[0] == "root"), None)
    if root_acct is None or root_acct[2] != "/root":
        raise SanitizeError("staged /etc/passwd root account missing or home is not /root")
    root_home = _safe_path(rootfs, workspace, "/root")
    if not os.path.lexists(root_home):
        print("SKIPPED absent staged /root")
        return None
    if os.path.islink(root_home) or not os.path.isdir(root_home):
        raise SanitizeError("staged /root is not a directory: " + root_home)
    return root_home


def _build_root_home_plan(rootfs, workspace, root_home):
    if root_home is None:
        return []
    entries = os.listdir(root_home)
    if not entries:
        print("SKIPPED empty staged /root")
        return []
    plan = []
    for name in entries:
        rel = "root/" + name
        if rel in ROOT_KEEP_FILES:
            if os.path.lexists(os.path.join(root_home, name)):
                target = _safe_path(rootfs, workspace, "/" + rel)
                if not os.path.isfile(target):
                    raise SanitizeError("staged /%s is not a regular file: %s" % (rel, target))
            continue
        plan.append(_safe_path(rootfs, workspace, "/" + rel))
    return plan


def scrub_root_home(workspace, plan):
    for target in plan:
        if os.path.lexists(target):
            workspace.remove(target)


def _validate_hostname_targets(rootfs, workspace):
    paths = {}
    for rel in HOSTNAME_TARGETS:
        path = _safe_path(rootfs, workspace, "/" + rel)
        _validate_parent_dirs(rootfs, "/" + rel)
        if os.path.lexists(path) and not os.path.isfile(path):
            raise SanitizeError("staged /%s is not a regular file: %s" % (rel, path))
        paths[rel] = path
    return paths


def _validate_machine_id_paths(rootfs, workspace):
    machine_id = _safe_path(rootfs, workspace, "/etc/machine-id")
    _validate_parent_dirs(rootfs, "/etc/machine-id")
    if os.path.lexists(machine_id) and not os.path.isfile(machine_id):
        raise SanitizeError("staged /etc/machine-id is not a regular file: " + machine_id)
    dbus_id = _safe_path(rootfs, workspace, "/var/lib/dbus/machine-id")
    _validate_parent_dirs(rootfs, "/var/lib/dbus/machine-id")
    if os.path.lexists(dbus_id) and not os.path.isfile(dbus_id):
        raise SanitizeError("staged /var/lib/dbus/machine-id is not a regular file: "
                            + dbus_id)
    return machine_id, dbus_id


def _build_removal_plan(rootfs, workspace, accounts, paths):
    preserved_homes = set()
    for _n, uid, home_dir in accounts:
        if uid < MIN_USER_UID:
            # "/" cannot anchor overlap checks; user homes at protected
            # locations remain blocked by _check_home_allowed.
            if home_dir != "/" and not _home_in_protected_location(home_dir):
                target = _safe_path(rootfs, workspace, home_dir)
                _validate_parent_dirs(rootfs, home_dir)
                if os.path.lexists(target):
                    preserved_homes.add(os.path.realpath(target))
    users = [(name, home_dir) for name, uid, home_dir in accounts
             if uid >= MIN_USER_UID and not _home_in_protected_location(home_dir)]
    plan = []
    for name, home_dir in users:
        targets = [_home_target(rootfs, workspace, home_dir, preserved_homes)]
        plan.append((name, targets))
    return plan


def _plan_ssh_host_keys(rootfs, workspace):
    ssh_dir = _safe_path(rootfs, workspace, "/etc/ssh")
    _validate_parent_dirs(rootfs, "/etc/ssh")
    if not os.path.lexists(ssh_dir):
        return []
    if not os.path.isdir(ssh_dir):
        raise SanitizeError("staged /etc/ssh is not a directory: " + ssh_dir)
    plan = []
    for name in os.listdir(ssh_dir):
        if not name.startswith(SSH_HOST_KEY_GLOB):
            continue
        child = _safe_path(rootfs, workspace, "/etc/ssh/" + name)
        if not os.path.isfile(child):
            raise SanitizeError("staged SSH host-key entry is not a regular file: " + child)
        plan.append(child)
    return plan


def _plan_clear_dir(rootfs, workspace, rel):
    target = _safe_path(rootfs, workspace, "/" + rel)
    _validate_parent_dirs(rootfs, "/" + rel)
    if not os.path.lexists(target):
        return []
    if not os.path.isdir(target):
        raise SanitizeError("staged /%s is not a directory: %s" % (rel, target))
    plan = []
    for name in os.listdir(target):
        plan.append(_safe_path(rootfs, workspace, "/" + rel + "/" + name))
    return plan


def _plan_clear_tree_files(rootfs, workspace, rel):
    target = _safe_path(rootfs, workspace, "/" + rel)
    _validate_parent_dirs(rootfs, "/" + rel)
    if not os.path.lexists(target):
        return []
    if os.path.islink(target) or not os.path.isdir(target):
        raise SanitizeError("staged /%s is not a directory: %s" % (rel, target))
    plan = []

    def _onerror(err):
        raise SanitizeError("cannot traverse staged /%s: %s" % (rel, err))

    for dirpath, dirnames, filenames in os.walk(target, followlinks=False,
                                                onerror=_onerror):
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            if os.path.isdir(full) and not os.path.islink(full):
                continue
            plan.append(_safe_path(rootfs, workspace,
                                   "/" + os.path.relpath(full, rootfs),
                                   allow_symlink_leaf=True))
    return plan


def _remove_targets(workspace, targets):
    for target in targets:
        if os.path.lexists(target):
            workspace.remove(target)


WG_INIT_PREFIX = "wg-quick."
UPDATE_RCD_CANDIDATES = ("/usr/sbin/update-rc.d", "/sbin/update-rc.d")
INSSERV_CANDIDATES = ("/sbin/insserv", "/usr/sbin/insserv", "/usr/bin/insserv",
                      "/usr/lib/insserv/insserv")


def _staged_tool(rootfs, candidates):
    for rel in candidates:
        path = os.path.join(rootfs, rel.lstrip("/"))
        if os.path.isfile(path) and not os.path.islink(path) and os.access(path, os.X_OK):
            return rel
    return None


def _depend_file_names(rootfs, workspace):
    initd = _safe_path(rootfs, workspace, "/etc/init.d")
    if os.path.islink(initd) or not os.path.isdir(initd):
        return []
    return sorted(n for n in os.listdir(initd) if n.startswith(".depend."))


def _find_custom_wg_services(rootfs, workspace):
    initd = _safe_path(rootfs, workspace, "/etc/init.d")
    _validate_parent_dirs(rootfs, "/etc/init.d")
    if os.path.islink(initd) or not os.path.isdir(initd):
        return []
    services = []
    for name in sorted(os.listdir(initd)):
        if not name.startswith(WG_INIT_PREFIX) or len(name) == len(WG_INIT_PREFIX):
            continue
        st = os.lstat(os.path.join(initd, name))
        if stat.S_ISLNK(st.st_mode):
            raise SanitizeError("staged /etc/init.d/%s is a symlink, refusing" % name)
        if not stat.S_ISREG(st.st_mode):
            raise SanitizeError("staged /etc/init.d/%s is not a regular file, refusing"
                                % name)
        services.append(name)
    return services


def _rc_links_to(rootfs, workspace, service):
    etc = _safe_path(rootfs, workspace, "/etc")
    _validate_parent_dirs(rootfs, "/etc")
    target = os.path.join(rootfs, "etc", "init.d", service)
    links = []
    for entry in sorted(os.listdir(etc)):
        if not (entry.startswith("rc") and entry.endswith(".d")):
            continue
        rcdir = os.path.join(etc, entry)
        if os.path.islink(rcdir) or not os.path.isdir(rcdir):
            continue
        for name in sorted(os.listdir(rcdir)):
            path = os.path.join(rcdir, name)
            if os.path.islink(path) \
                    and os.path.normpath(os.path.join(rcdir, os.readlink(path))) == target:
                links.append("/etc/%s/%s" % (entry, name))
    return links


def _depend_references(rootfs, workspace, service):
    refs = []
    for fname in _depend_file_names(rootfs, workspace):
        fpath = _safe_path(rootfs, workspace, "/etc/init.d/" + fname)
        st = os.lstat(fpath)
        if not stat.S_ISREG(st.st_mode):
            raise SanitizeError("staged /etc/init.d/%s is not a regular file, refusing"
                                % fname)
        with open(fpath, "r", errors="replace") as fh:
            for lineno, line in enumerate(fh, 1):
                if service in line.replace(":", " ").split():
                    refs.append("%s:%d" % (fname, lineno))
    return refs


def _remove_custom_wg_services(rootfs, workspace):
    services = _find_custom_wg_services(rootfs, workspace)
    if not services:
        return []
    update_rc_d = _staged_tool(rootfs, UPDATE_RCD_CANDIDATES)
    if update_rc_d is None:
        raise SanitizeError("staged update-rc.d not found (%s); refusing to remove "
                            "custom WireGuard services"
                            % ", ".join(UPDATE_RCD_CANDIDATES))
    if _depend_file_names(rootfs, workspace):
        insserv = _staged_tool(rootfs, INSSERV_CANDIDATES)
        inserv_conf = os.path.join(rootfs, "etc", "insserv.conf")
        if insserv is None or os.path.islink(inserv_conf) or not os.path.isfile(inserv_conf):
            raise SanitizeError("staged /etc/init.d contains generated .depend.* files "
                                "but the staged insserv backend is unavailable "
                                "(insserv executable or /etc/insserv.conf missing); "
                                "refusing the plain-symlink fallback")
    env = dict(os.environ)
    env.pop("DPKG_ROOT", None)
    removed = []
    for name in services:
        proc = subprocess.run(["chroot", rootfs, update_rc_d, "-f", name, "remove"],
                              capture_output=True, text=True, env=env)
        if proc.returncode != 0:
            raise SanitizeError("chroot %s %s -f %s remove failed (exit %d): %s"
                                % (rootfs, update_rc_d, name, proc.returncode,
                                   proc.stderr.strip()))
        links = _rc_links_to(rootfs, workspace, name)
        if links:
            raise SanitizeError("runlevel registrations still reference %s: %s"
                                % (name, ", ".join(links)))
        refs = _depend_references(rootfs, workspace, name)
        if refs:
            raise SanitizeError("generated dependency references still mention %s: %s"
                                % (name, ", ".join(refs)))
        script = _safe_path(rootfs, workspace, "/etc/init.d/" + name)
        if os.path.islink(script) or not os.path.isfile(script):
            raise SanitizeError("staged /etc/init.d/%s missing or not a regular file"
                                % name)
        workspace.remove(script)
        removed.append(name)
    return removed


def _filter_account_lines(lines, drop_user):
    return [ln for ln in lines if ln.split(":")[0] != drop_user]


def _filter_group_lines(lines, drop_user, scrub_admins):
    out = []
    for ln in lines:
        fields = ln.split(":")
        if fields and fields[0] == drop_user:
            continue
        if scrub_admins and len(fields) >= 3 and drop_user in fields[2].split(","):
            fields[2] = ",".join(m for m in fields[2].split(",") if m != drop_user)
        if len(fields) >= 4 and drop_user in fields[3].split(","):
            fields[3] = ",".join(m for m in fields[3].split(",") if m != drop_user)
        out.append(":".join(fields))
    return out


def _execute_removal_plan(paths, contents, plan, workspace):
    current = dict(contents)
    for name, targets in plan:
        current["etc/passwd"] = _filter_account_lines(current["etc/passwd"], name)
        current["etc/shadow"] = _filter_account_lines(current["etc/shadow"], name)
        current["etc/group"] = _filter_group_lines(current["etc/group"], name,
                                                   scrub_admins=False)
        current["etc/gshadow"] = _filter_group_lines(current["etc/gshadow"], name,
                                                     scrub_admins=True)
        for rel in ACCOUNT_FILES:
            _write_lines(paths[rel], [ln + "\n" for ln in current[rel]],
                         "account file")
        for target in targets:
            if os.path.lexists(target):
                workspace.remove(target)
    return [name for name, _t in plan]


def _plan_target(rootfs, workspace, rel):
    path = _safe_path(rootfs, workspace, "/" + rel)
    _validate_parent_dirs(rootfs, "/" + rel)
    if not os.path.lexists(path):
        return path, None
    return path, os.lstat(path)


def _assert_clean_tree(path):
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            st = os.lstat(full)
            if not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
                raise SanitizeError("unexpected object type in staged cleanup tree, "
                                    "refusing: " + full)


def _plan_resolv_conf(rootfs, workspace):
    etc = _safe_path(rootfs, workspace, "/etc")
    _validate_parent_dirs(rootfs, "/etc/resolv.conf")
    path = os.path.join(etc, "resolv.conf")
    if not os.path.lexists(path):
        return ("keep", path)
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        return ("keep", path)
    if stat.S_ISREG(st.st_mode):
        return ("truncate", path)
    raise SanitizeError("staged /etc/resolv.conf is an unexpected file type: " + path)


def _execute_resolv_conf(action, path):
    if action == "keep":
        return
    if action == "truncate":
        _write_lines(path, [], "resolv.conf")
        return
    raise SanitizeError("unknown resolv.conf action: %r" % action)


def _plan_regular_file_removals(rootfs, workspace, rels):
    plan = []
    for rel in rels:
        path, st = _plan_target(rootfs, workspace, rel)
        if st is None:
            continue
        if not stat.S_ISREG(st.st_mode):
            raise SanitizeError("staged /%s is not a regular file: %s" % (rel, path))
        plan.append(path)
    return plan


def _valid_creator_name(creator):
    return (bool(creator) and "\x00" not in creator and "/" not in creator
            and creator not in (".", ".."))


_ALIAS_KEYWORDS = ("Runas_Alias", "Cmnd_Alias", "Host_Alias")


def _split_member_list(field):
    return [tok.strip() for tok in field.split(",") if tok.strip()]


def _split_leading_list(text):
    tokens = text.split()
    if not tokens:
        raise SanitizeError("cannot parse sudoers list: %r" % text)
    i = 0
    while i < len(tokens) - 1 and (tokens[i].endswith(",") or tokens[i + 1].startswith(",")):
        i += 1
    return " ".join(tokens[:i + 1]), " ".join(tokens[i + 1:])


def _is_creator_token(tok, creator, uid):
    t = tok.lstrip("!")
    if t == creator:
        return True
    return uid is not None and t == "#" + str(uid)


def _split_comment(ln):
    for i, ch in enumerate(ln):
        if ch == "#" and not (i + 1 < len(ln) and ln[i + 1].isdigit()):
            return ln[:i], ln[i:]
    return ln, ""


def _scrub_member_list(entries, creator, uid):
    for e in entries:
        if "(" in e or '"' in e or "\\\\" in e:
            raise SanitizeError("malformed sudoers list member %r, refusing" % e)
        t = e.lstrip("!")
        if uid is None and t.startswith("#") and t[1:].isdigit():
            raise SanitizeError("sudoers numeric UID token %r with unknown creator uid, "
                                "refusing" % e)
    kept = [e for e in entries if not _is_creator_token(e, creator, uid)]
    return kept, len(kept) != len(entries)


def _scrub_sudoers_line(ln, creator, uid):
    s = ln.strip()
    if not s or (s.startswith("#") and not s[1:2].isdigit()):
        return ln, "keep"
    if s.endswith("\\\\"):
        raise SanitizeError("sudoers backslash-continued line, refusing to parse: %r" % ln)
    body, comment = _split_comment(ln)
    stripped = body.strip()
    if any(stripped.startswith(k) for k in _ALIAS_KEYWORDS):
        return ln, "keep"
    if stripped.startswith("User_Alias"):
        rest = stripped[len("User_Alias"):].strip()
        new_defs = []
        changed = False
        for d in rest.split(":"):
            if "=" not in d:
                raise SanitizeError("malformed User_Alias definition, refusing: %r" % ln)
            name, _, members_str = d.partition("=")
            entries = _split_member_list(members_str)
            kept, did = _scrub_member_list(entries, creator, uid)
            if did:
                changed = True
                if not kept:
                    raise SanitizeError("User_Alias %s would become empty after removing "
                                        "%r, refusing: %r" % (name.strip(), creator, ln))
            new_defs.append("%s = %s" % (name.strip(), ", ".join(kept)))
        if not changed:
            return ln, "keep"
        out = "User_Alias " + " : ".join(new_defs)
        if comment:
            out += " " + comment
        return out, "keep"
    if stripped.startswith("Defaults"):
        after = stripped[len("Defaults"):]
        if after[:1] in (":", ">"):
            list_str, tail = _split_leading_list(after[1:].strip())
            entries = _split_member_list(list_str)
            kept, did = _scrub_member_list(entries, creator, uid)
            if not did:
                return ln, "keep"
            if not kept:
                return None, "drop"
            out = "Defaults" + after[:1] + ", ".join(kept)
            if tail:
                out += " " + tail
            if comment:
                out += " " + comment
            return out, "keep"
        return ln, "keep"
    list_str, rest = _split_leading_list(stripped)
    entries = _split_member_list(list_str)
    kept, did = _scrub_member_list(entries, creator, uid)
    if not did:
        return ln, "keep"
    if not kept:
        return None, "drop"
    out = ", ".join(kept)
    if rest:
        out += " " + rest
    if comment:
        out += " " + comment
    return out, "keep"


def _plan_sudoers(rootfs, workspace, creator, uid):
    sudoers_path, st = _plan_target(rootfs, workspace, "etc/sudoers")
    rewrite = None
    if st is not None:
        if not stat.S_ISREG(st.st_mode):
            raise SanitizeError("staged /etc/sudoers is not a regular file: " + sudoers_path)
        kept = []
        for ln in _read_lines(sudoers_path):
            new_ln, action = _scrub_sudoers_line(ln, creator, uid)
            if action == "drop":
                continue
            kept.append(new_ln if new_ln is not None else ln)
        rewrite = (sudoers_path, kept)
    drop = None
    dpath, dst_st = _plan_target(rootfs, workspace, "etc/sudoers.d")
    if dst_st is not None:
        if not stat.S_ISDIR(dst_st.st_mode):
            raise SanitizeError("staged /etc/sudoers.d is not a directory: " + dpath)
        target = os.path.join(dpath, creator)
        if os.path.lexists(target):
            tst = os.lstat(target)
            if not stat.S_ISREG(tst.st_mode):
                raise SanitizeError("staged /etc/sudoers.d/%s is not a regular file: %s"
                                    % (creator, target))
            if not workspace.contains(target):
                raise SanitizeError("sudoers.d entry outside workspace: " + target)
            drop = target
    return rewrite, drop


def _plan_subid_files(rootfs, workspace, creator):
    rewrites = []
    for rel in SUBID_FILES:
        path, st = _plan_target(rootfs, workspace, rel)
        if st is None:
            continue
        if not stat.S_ISREG(st.st_mode):
            raise SanitizeError("staged /%s is not a regular file: %s" % (rel, path))
        kept = [ln for ln in _read_lines(path) if ln.split(":", 1)[0] != creator]
        rewrites.append((path, kept))
    return rewrites


def _plan_dir_children_removal(rootfs, workspace, rel, keep=()):
    path, st = _plan_target(rootfs, workspace, rel)
    if st is None:
        return [], None
    if not stat.S_ISDIR(st.st_mode):
        raise SanitizeError("staged /%s is not a directory: %s" % (rel, path))
    plan = []
    kept_dirs = []
    for name in sorted(os.listdir(path)):
        child = os.path.join(path, name)
        cst = os.lstat(child)
        if stat.S_ISLNK(cst.st_mode):
            raise SanitizeError("symlink inside staged /%s, refusing: %s" % (rel, child))
        if name in keep:
            if not stat.S_ISDIR(cst.st_mode):
                raise SanitizeError("staged /%s/%s is not a directory: %s"
                                    % (rel, name, child))
            kept_dirs.append(child)
            continue
        if stat.S_ISDIR(cst.st_mode):
            _assert_clean_tree(child)
            plan.append(child)
        elif stat.S_ISREG(cst.st_mode):
            plan.append(child)
        else:
            raise SanitizeError("unexpected object type in staged /%s, refusing: %s"
                                % (rel, child))
    return plan, kept_dirs


def _plan_nm_state(rootfs, workspace):
    path, st = _plan_target(rootfs, workspace, NM_STATE_DIR)
    if st is None:
        return []
    if not stat.S_ISDIR(st.st_mode):
        raise SanitizeError("staged /%s is not a directory: %s" % (NM_STATE_DIR, path))
    plan = []
    for name in sorted(os.listdir(path)):
        child = os.path.join(path, name)
        cst = os.lstat(child)
        if stat.S_ISLNK(cst.st_mode):
            raise SanitizeError("symlink inside staged /%s, refusing: %s"
                                % (NM_STATE_DIR, child))
        if name in NM_REMOVED_FILES or (name.startswith(NM_LEASE_PREFIX)
                                        and name.endswith(".lease")):
            if not stat.S_ISREG(cst.st_mode):
                raise SanitizeError("staged /%s/%s is not a regular file: %s"
                                    % (NM_STATE_DIR, name, child))
            plan.append(child)
        else:
            raise SanitizeError("unexpected entry in staged /%s, refusing: %s"
                                % (NM_STATE_DIR, child))
    return plan


def _plan_apt_lists(rootfs, workspace, accounts):
    apt_uid = next((u for n, u, _h in accounts if n == "_apt"), 0)
    lists_path, st = _plan_target(rootfs, workspace, APT_LISTS_DIR)
    partial_path = os.path.join(lists_path, "partial")
    plan = {"lists_path": lists_path, "partial_path": partial_path,
            "removals": [], "ensure_lists": False, "ensure_partial": False,
            "normalize": []}
    if st is None:
        plan["ensure_lists"] = True
        plan["ensure_partial"] = True
        plan["normalize"].append((lists_path, 0o755, 0, 0))
        plan["normalize"].append((partial_path, 0o700, apt_uid, 0))
        return plan
    if not stat.S_ISDIR(st.st_mode):
        raise SanitizeError("staged /%s is not a directory: %s" % (APT_LISTS_DIR, lists_path))
    plan["normalize"].append((lists_path, 0o755, 0, 0))
    seen = set()
    for name in sorted(os.listdir(lists_path)):
        seen.add(name)
        child = os.path.join(lists_path, name)
        cst = os.lstat(child)
        if stat.S_ISLNK(cst.st_mode):
            raise SanitizeError("symlink inside staged /%s, refusing: %s"
                                % (APT_LISTS_DIR, child))
        if name == "lock":
            if not stat.S_ISREG(cst.st_mode):
                raise SanitizeError("staged /%s/lock is not a regular file: %s"
                                    % (APT_LISTS_DIR, child))
            continue
        if name in ("partial", "auxfiles"):
            if not stat.S_ISDIR(cst.st_mode):
                raise SanitizeError("staged /%s/%s is not a directory: %s"
                                    % (APT_LISTS_DIR, name, child))
            for sub in sorted(os.listdir(child)):
                schild = os.path.join(child, sub)
                sst = os.lstat(schild)
                if not (stat.S_ISREG(sst.st_mode) or stat.S_ISDIR(sst.st_mode)):
                    raise SanitizeError("unexpected object type in staged /%s/%s, "
                                        "refusing: %s" % (APT_LISTS_DIR, name, schild))
                if stat.S_ISDIR(sst.st_mode):
                    _assert_clean_tree(schild)
                plan["removals"].append(schild)
            plan["normalize"].append((child, 0o700 if name == "partial" else 0o755,
                                      apt_uid, 0))
            continue
        if stat.S_ISDIR(cst.st_mode):
            _assert_clean_tree(child)
            plan["removals"].append(child)
        elif stat.S_ISREG(cst.st_mode):
            plan["removals"].append(child)
        else:
            raise SanitizeError("unexpected object type in staged /%s, refusing: %s"
                                % (APT_LISTS_DIR, child))
    if "partial" not in seen:
        plan["ensure_partial"] = True
        plan["normalize"].append((partial_path, 0o700, apt_uid, 0))
    return plan


def _execute_apt_lists(plan, workspace):
    if plan["ensure_lists"] or plan["ensure_partial"]:
        try:
            os.makedirs(plan["partial_path"], exist_ok=True)
        except OSError as exc:
            raise SanitizeError("cannot create staged /var/lib/apt/lists/partial: %s" % exc)
    _remove_targets(workspace, plan["removals"])
    for path, mode, uid, gid in plan["normalize"]:
        try:
            os.chmod(path, mode)
            os.chown(path, uid, gid)
        except OSError as exc:
            raise SanitizeError("cannot normalize staged %s: %s" % (path, exc))


def sanitize(workspace, creator):
    if not creator:
        raise SanitizeError("refusing to sanitize without a creator account")
    if not _valid_creator_name(creator):
        raise SanitizeError("unsafe creator account name: %r" % creator)
    rootfs = _validate_rootfs(workspace)
    paths = {rel: _account_path(rootfs, workspace, rel) for rel in ACCOUNT_FILES}
    contents = {rel: _read_lines(paths[rel]) for rel in ACCOUNT_FILES}
    accounts = _parse_passwd(contents["etc/passwd"])
    _parse_shadow(paths["etc/shadow"])
    _parse_grouplike(paths["etc/group"], "/etc/group")
    _parse_grouplike(paths["etc/gshadow"], "/etc/gshadow")
    if creator in PROTECTED_ACCOUNTS:
        raise SanitizeError("refusing to remove protected account: %r" % creator)
    uid = next((u for n, u, _h in accounts if n == creator), None)
    if uid is not None and uid < MIN_USER_UID:
        raise SanitizeError("refusing to remove system account (uid %d): %r" % (uid, creator))
    machine_id, dbus_id = _validate_machine_id_paths(rootfs, workspace)
    fstab_crypttab = _validate_fstab_crypttab(rootfs, workspace)
    hostname_paths = _validate_hostname_targets(rootfs, workspace)
    root_home = _validate_root_home(rootfs, workspace, accounts)
    root_plan = _build_root_home_plan(rootfs, workspace, root_home)
    plan = _build_removal_plan(rootfs, workspace, accounts, paths)
    ssh_plan = _plan_ssh_host_keys(rootfs, workspace)
    clear_plans = [_plan_clear_dir(rootfs, workspace, rel)
                   for rel in NETWORK_CREDENTIAL_DIRS + CACHE_DIRS]
    clear_plans += [_plan_clear_tree_files(rootfs, workspace, rel) for rel in LOG_DIRS]
    hostname_contents = {
        "etc/hostname": [HOSTNAME_VALUE + "\n"],
        "etc/hosts": [ln + "\n" for ln in HOSTS_SKELETON_LINES],
        "etc/machine-info": [],
    }
    resolv_plan = _plan_resolv_conf(rootfs, workspace)
    backup_plan = _plan_regular_file_removals(rootfs, workspace, ACCOUNT_BACKUP_FILES)
    sudoers_rewrite, sudoers_drop = _plan_sudoers(rootfs, workspace, creator, uid)
    subid_rewrites = _plan_subid_files(rootfs, workspace, creator)
    nm_plan = _plan_nm_state(rootfs, workspace)
    dhcp_plan, _dhcp_kept = _plan_dir_children_removal(rootfs, workspace, DHCP_STATE_DIR)
    apt_plan = _plan_apt_lists(rootfs, workspace, accounts)
    grub_plan = _plan_regular_file_removals(rootfs, workspace, GRUB_GENERATED_FILES)
    _write_lines(machine_id, [], "machine-id")
    if os.path.lexists(dbus_id):
        workspace.remove(dbus_id)
    os.makedirs(os.path.dirname(dbus_id), exist_ok=True)
    os.symlink("/etc/machine-id", dbus_id)
    for rel in HOSTNAME_TARGETS:
        path = hostname_paths[rel]
        if not os.path.lexists(path):
            print("SKIPPED absent staged /%s" % rel)
            continue
        _write_lines(path, hostname_contents[rel], rel)
    _remove_targets(workspace, ssh_plan)
    for targets in clear_plans:
        _remove_targets(workspace, targets)
    _remove_custom_wg_services(rootfs, workspace)
    _execute_removal_plan(paths, contents, plan, workspace)
    _write_lines(fstab_crypttab["etc/fstab"], [], "fstab")
    crypttab = fstab_crypttab["etc/crypttab"]
    if os.path.lexists(crypttab):
        workspace.remove(crypttab)
    scrub_root_home(workspace, root_plan)
    _execute_resolv_conf(*resolv_plan)
    _remove_targets(workspace, backup_plan)
    if sudoers_rewrite is not None:
        spath, kept = sudoers_rewrite
        _write_lines(spath, [ln + "\n" for ln in kept], "sudoers")
    if sudoers_drop is not None:
        _remove_targets(workspace, [sudoers_drop])
    for spath, kept in subid_rewrites:
        _write_lines(spath, [ln + "\n" for ln in kept], os.path.basename(spath))
    _remove_targets(workspace, nm_plan)
    _remove_targets(workspace, dhcp_plan)
    _execute_apt_lists(apt_plan, workspace)
    _remove_targets(workspace, grub_plan)
    return rootfs