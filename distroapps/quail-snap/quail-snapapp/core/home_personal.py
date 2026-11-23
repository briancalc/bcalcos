import os
import pwd
import re

from core import home
from core import sanitize_personal as sp

LIVEBOOT_BOOT_FILES = (
    "usr/share/bcalcosbrand/liveboot/grub/grub.cfg",
    "usr/share/bcalcosbrand/liveboot/isolinux/live.cfg",
)
APPLICATIONS_REL = "usr/share/applications"
LAUNCHER_DESKTOP_FILES = sp.DESKTOP_LAUNCHER_FILES
USERNAME_PARAM_RE = re.compile(r"username=\S+")


class HomePersonalError(Exception):
    pass


def _invoking_user():
    sudo_uid = os.environ.get("SUDO_UID")
    if not sudo_uid or not sudo_uid.isdigit():
        raise HomePersonalError("cannot resolve invoking user: SUDO_UID is not set")
    try:
        return pwd.getpwuid(int(sudo_uid))
    except KeyError:
        raise HomePersonalError("cannot resolve invoking user: no account for uid %s"
                                % sudo_uid)


def _valid_creator_name(creator):
    if not creator or "\x00" in creator or "/" in creator or creator in (".", ".."):
        return False
    return not any(c in creator for c in " \t\n\r=/")


def creator_home(pw):
    expected = "/home/" + pw.pw_name
    if pw.pw_dir != expected:
        raise HomePersonalError("creator home %r does not match the standard layout "
                                "%r; refusing to capture" % (pw.pw_dir, expected))
    if os.path.islink(pw.pw_dir) or not os.path.isdir(pw.pw_dir):
        raise HomePersonalError("creator home is missing, a symlink, or not a "
                                "directory: " + pw.pw_dir)
    return pw.pw_dir


def _parse_passwd(path):
    accounts = {}
    with open(path, "r") as fh:
        for lineno, line in enumerate(fh, 1):
            fields = line.rstrip("\n").split(":")
            if len(fields) != 7 or not fields[0]:
                raise HomePersonalError("malformed staged /etc/passwd line %d "
                                        "(contents withheld)" % lineno)
            if not fields[2].isdigit() or not fields[3].isdigit():
                raise HomePersonalError("malformed uid/gid in staged /etc/passwd line %d "
                                        "(contents withheld)" % lineno)
            accounts[fields[0]] = (int(fields[2]), int(fields[3]), fields[5])
    return accounts


def _staged_file(rootfs, workspace, rel):
    path = sp._safe_path(rootfs, workspace, "/" + rel)
    sp._validate_parent_dirs(rootfs, "/" + rel)
    if not os.path.isfile(path):
        raise HomePersonalError("staged /%s missing or not a regular file: %s"
                                % (rel, path))
    return path


def _configure_liveboot_file(rootfs, workspace, rel, creator):
    path = _staged_file(rootfs, workspace, rel)
    with open(path, "r") as fh:
        text = fh.read()
    lines = text.split("\n")
    out = []
    changed = 0
    for ln in lines:
        if ln.lstrip().startswith("#") or "username=" not in ln:
            out.append(ln)
            continue
        new_ln = USERNAME_PARAM_RE.sub("username=" + creator, ln)
        if new_ln != ln:
            changed += 1
        out.append(new_ln)
    active_values = [USERNAME_PARAM_RE.search(ln).group(0)
                     for ln in out
                     if "username=" in ln and not ln.lstrip().startswith("#")]
    if not active_values:
        raise HomePersonalError("no active username= boot parameter in staged /%s" % rel)
    if any(v != "username=" + creator for v in active_values):
        raise HomePersonalError("failed to set username=%s in all active entries of "
                                "staged /%s" % (creator, rel))
    if changed:
        with open(path, "w") as fh:
            fh.write("\n".join(out))
    return changed


def _same_content(a, b):
    if os.path.islink(a) or os.path.islink(b):
        return False
    if not (os.path.isfile(a) and os.path.isfile(b)):
        return False
    return sp.sha256_file(a) == sp.sha256_file(b)


def _ensure_desktop(rootfs, workspace, home_dir, uid, gid):
    home = sp._safe_path(rootfs, workspace, home_dir)
    sp._validate_parent_dirs(rootfs, home_dir)
    if not os.path.lexists(home) or os.path.islink(home) or not os.path.isdir(home):
        raise HomePersonalError("staged creator home missing or not a real directory: "
                                + home)
    desktop = sp._safe_path(rootfs, workspace, home_dir + "/Desktop")
    sp._validate_parent_dirs(rootfs, home_dir + "/Desktop")
    created = False
    if os.path.lexists(desktop):
        if os.path.islink(desktop) or not os.path.isdir(desktop):
            raise HomePersonalError("staged Desktop is not a directory: " + desktop)
    else:
        os.mkdir(desktop)
        os.chown(desktop, uid, gid)
        created = True
    return desktop, created


def _place_launcher(rootfs, workspace, desktop, name, uid, gid):
    src = _staged_file(rootfs, workspace, APPLICATIONS_REL + "/" + name)
    dest = os.path.join(desktop, name)
    if os.path.lexists(dest):
        if _same_content(src, dest):
            return dest, "kept"
        base = "quail-snap-" + name
        cand = os.path.join(desktop, base)
        i = 2
        while os.path.lexists(cand) and not _same_content(src, cand):
            cand = os.path.join(desktop, "%s-%d" % (base, i))
            i += 1
        if os.path.lexists(cand) and _same_content(src, cand):
            return cand, "kept"
        dest = cand
    with open(src, "rb") as fh:
        data = fh.read()
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o755)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.chmod(dest, 0o755)
    os.chown(dest, uid, gid)
    return dest, "placed"


def _read_lines(path):
    with open(path, "r") as fh:
        return fh.read().splitlines()


def _read_text(path):
    with open(path, "r") as fh:
        return fh.read()


def _write_lines(path, lines):
    with open(path, "w") as fh:
        fh.writelines(ln + "\n" for ln in lines)


def _move_home(rootfs, workspace, old_home, live_home):
    if not old_home.startswith("/home/") or old_home == "/home/":
        raise HomePersonalError("creator home %r is not inside the captured /home tree"
                                % old_home)
    src = sp._safe_path(rootfs, workspace, old_home)
    sp._validate_parent_dirs(rootfs, old_home)
    if not os.path.isdir(src) or os.path.islink(src):
        raise HomePersonalError("staged creator home missing or not a real directory: "
                                + src)
    home_real = os.path.realpath(os.path.join(rootfs, "home"))
    src_real = os.path.realpath(src)
    if src_real == home_real or not src_real.startswith(home_real + os.sep):
        raise HomePersonalError("creator home escapes the captured /home tree: "
                                + old_home)
    dst = sp._safe_path(rootfs, workspace, live_home)
    sp._validate_parent_dirs(rootfs, live_home)
    if os.path.lexists(dst):
        raise HomePersonalError("staged %s already exists, refusing to overwrite or merge"
                                % live_home)
    before = sp._tree_manifest(src)
    try:
        os.rename(src, dst)
    except OSError as exc:
        raise HomePersonalError("cannot move home to %s: %s" % (live_home, exc))
    after = sp._tree_manifest(dst)
    if before != after or os.path.lexists(src):
        try:
            os.rename(dst, src)
        except OSError:
            pass
        raise HomePersonalError("home move verification failed for %s" % live_home)
    return dst


def _remove_login_keyrings(rootfs, workspace, home_abs):
    kdir = sp._safe_path(rootfs, workspace,
                         home_abs + "/" + sp.KEYRING_DIR_REL, allow_symlink_leaf=True)
    if not os.path.lexists(kdir):
        return []
    if not os.path.isdir(kdir) or os.path.islink(kdir):
        raise HomePersonalError("staged keyring directory is not a real directory: " + kdir)
    removed = []
    for name in sorted(os.listdir(kdir)):
        if not name.endswith(".keyring"):
            continue
        child = sp._safe_path(rootfs, workspace,
                              home_abs + "/" + sp.KEYRING_DIR_REL + "/" + name)
        if os.path.islink(child) or not os.path.isfile(child):
            raise HomePersonalError("staged keyring entry is not a regular file: " + child)
        workspace.remove(child)
        removed.append(name)
    return removed


def _normalize_account(workspace, rootfs, creator):
    if not _valid_creator_name(creator):
        raise HomePersonalError("unsafe creator account name: %r" % creator)
    live = home.LIVE_USER
    live_home = "/home/" + live
    passwd_path = sp._safe_path(rootfs, workspace, "/etc/passwd")
    sp._validate_parent_dirs(rootfs, "/etc/passwd")
    if not os.path.isfile(passwd_path):
        raise HomePersonalError("staged /etc/passwd missing or not a regular file")
    accounts = _parse_passwd(passwd_path)
    if creator not in accounts:
        raise HomePersonalError("creator account %r missing from staged /etc/passwd"
                                % creator)
    uid, gid, old_home = accounts[creator]
    if live in accounts and live != creator:
        raise HomePersonalError("staged account %r already exists, refusing" % live)
    if creator == live and old_home != live_home:
        raise HomePersonalError("creator is already %r but its home is %r; "
                                "inconsistent account configuration, refusing"
                                % (live, old_home))
    for name, (u, _g, _h) in accounts.items():
        if name != creator and u == uid:
            raise HomePersonalError("uid %d is shared by %r and %r, refusing to normalize"
                                    % (uid, creator, name))
    shadow_path = _staged_file(rootfs, workspace, "etc/shadow")
    group_path = _staged_file(rootfs, workspace, "etc/group")
    gshadow_path = _staged_file(rootfs, workspace, "etc/gshadow")
    group_lines = _read_lines(group_path)
    gid_name = None
    for ln in group_lines:
        f = ln.split(":")
        if len(f) >= 3 and f[2] == str(gid):
            gid_name = f[0]
            break
    if gid_name is None:
        raise HomePersonalError("no group with creator gid %d in staged /etc/group" % gid)
    rename_group = gid_name == creator
    for ln in group_lines:
        f = ln.split(":")
        if f and f[0] == live and f[0] != creator:
            raise HomePersonalError("staged group %r already exists, refusing" % live)

    password_hash = home._yescrypt_hash(home.LIVE_USER_PASSWORD)
    new_passwd = sp.xf_passwd_lines(_read_lines(passwd_path), creator, live, live_home)
    new_shadow = sp.xf_shadow_lines(_read_lines(shadow_path), creator, live,
                                    password_hash=password_hash)
    new_group = sp.xf_group_lines(group_lines, creator, live,
                                  rename_gid=gid if rename_group else None)
    new_gshadow = sp.xf_gshadow_lines(_read_lines(gshadow_path), creator, live,
                                      rename_group=rename_group)

    subid_plans = []
    for rel in ("etc/subuid", "etc/subgid"):
        p = sp._plan_target(rootfs, workspace, rel)
        if os.path.lexists(p):
            f = _staged_file(rootfs, workspace, rel)
            subid_plans.append((f, sp.xf_subid_lines(_read_lines(f), creator, live)))
    sudoers_plan = None
    spath = sp._plan_target(rootfs, workspace, "etc/sudoers")
    if os.path.lexists(spath):
        sf = _staged_file(rootfs, workspace, "etc/sudoers")
        sudoers_plan = (sf, sp.xf_sudoers_text(_read_text(sf), creator, live))

    dpath = sp._plan_target(rootfs, workspace, "etc/sudoers.d")
    droot_renames = []
    dcontents = {}
    if os.path.lexists(dpath):
        if os.path.islink(dpath) or not os.path.isdir(dpath):
            raise HomePersonalError("staged /etc/sudoers.d is not a real directory")
        for name in sorted(os.listdir(dpath)):
            child = os.path.join(dpath, name)
            if os.path.islink(child) or not os.path.isfile(child):
                raise HomePersonalError("staged /etc/sudoers.d/%s is not a regular file"
                                        % name)
            dcontents[child] = sp.xf_sudoers_text(_read_text(child), creator, live)
            if name == creator and creator != live:
                dst = sp._safe_path(rootfs, workspace, "/etc/sudoers.d/" + live)
                if os.path.lexists(dst):
                    raise HomePersonalError("staged /etc/sudoers.d/%s already exists"
                                            % live)
                droot_renames.append((child, dst))

    if creator != live:
        _move_home(rootfs, workspace, old_home, live_home)
    _write_lines(passwd_path, new_passwd)
    _write_lines(shadow_path, new_shadow)
    _write_lines(group_path, new_group)
    _write_lines(gshadow_path, new_gshadow)
    for f, lines in subid_plans:
        _write_lines(f, lines)
    if sudoers_plan is not None:
        with open(sudoers_plan[0], "w") as fh:
            fh.write(sudoers_plan[1])
    for child, text in dcontents.items():
        with open(child, "w") as fh:
            fh.write(text)
    for src, dst in droot_renames:
        os.rename(src, dst)
    return {"creator": creator, "live_user": live, "uid": uid, "gid": gid,
            "old_home": old_home, "new_home": live_home, "renamed_group": rename_group}


def prepare(workspace, creator=None):
    try:
        rootfs = sp._validate_rootfs(workspace)
        if creator is None:
            creator = _invoking_user().pw_name
        norm = _normalize_account(workspace, rootfs, creator)
        live = norm["live_user"]
        uid, gid, home_dir = norm["uid"], norm["gid"], norm["new_home"]
        liveboot = {}
        for rel in LIVEBOOT_BOOT_FILES:
            liveboot[rel] = _configure_liveboot_file(rootfs, workspace, rel, live)
        keyrings = _remove_login_keyrings(rootfs, workspace, home_dir)
        desktop, desktop_created = _ensure_desktop(rootfs, workspace, home_dir,
                                                    uid, gid)
        launchers = {}
        for name in LAUNCHER_DESKTOP_FILES:
            launchers[name] = _place_launcher(rootfs, workspace, desktop, name, uid, gid)
    except sp.SanitizePersonalError as exc:
        raise HomePersonalError(str(exc))
    return {"creator": creator, "live_user": live, "uid": uid, "gid": gid,
            "home": home_dir, "old_home": norm["old_home"],
            "keyrings_removed": keyrings, "liveboot": liveboot,
            "desktop": desktop, "desktop_created": desktop_created,
            "launchers": launchers}