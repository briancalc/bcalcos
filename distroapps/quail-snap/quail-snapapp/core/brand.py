import os
import shutil
import stat

from core import home
from core import sanitize

BRAND_SOURCE_REL = os.path.join("usr", "share", "bcalcosbrand", "isobuild")

ALLOWED_SUBPATHS = (
    ".config/BraveSoftware/Brave-Origin/Default/Bookmarks",
    ".config/BraveSoftware/Brave-Origin/Default/Extensions/ddkjiahejlhfcafbddmgiahcphecmpfh/",
)

SYSTEM_BRAND_SUBPATHS = (
    "etc/brave/policies/managed/ublock-origin-lite.json",
    "var/log/fsck/checkroot",
    "var/log/fsck/checkfs",
)
SYSTEM_PAYLOAD_REL = "system"

REQUIRED_SYSTEM_BRAND_SUBPATHS = (
    "var/log/fsck/checkroot",
    "var/log/fsck/checkfs",
)

RESERVED_SUBDIRS = ("liveboot",)

BLOCKED_NAMES = frozenset({
    "Cookies", "Login Data", "Login Data For Account", "Web Data", "History",
    "Top Sites", "Sessions", "Session Storage", "Local State", "Secure Preferences",
    "Sync Data", "Cache", "Code Cache", ".ssh", ".gnupg", ".bash_history",
})
BLOCKED_SUFFIXES = ("_history",)


class BrandError(Exception):
    pass


def _rootfs(workspace):
    return os.path.join(workspace.path, "staging", "rootfs")


def _audit_source(src_root):
    for dirpath, dirnames, filenames in os.walk(src_root):
        for name in dirnames + filenames:
            if name in BLOCKED_NAMES or name.endswith(BLOCKED_SUFFIXES):
                raise BrandError("personal-data artifact in brand source: "
                                 + os.path.join(dirpath, name))


def _brand_safe(rootfs, workspace, path, what):
    abs_path = "/" + os.path.relpath(path, rootfs)
    try:
        return sanitize._safe_path(rootfs, workspace, abs_path)
    except sanitize.SanitizeError as exc:
        raise BrandError("%s unsafe: %s" % (what, exc))


def _ensure_inside_home(dest, home_dir, what):
    dest_real = os.path.realpath(dest)
    home_real = os.path.realpath(home_dir)
    try:
        inside = (dest_real != home_real
                  and os.path.commonpath([home_real, dest_real]) == home_real)
    except ValueError:
        inside = False
    if not inside:
        raise BrandError("%s escapes live home %s: %s" % (what, home_dir, dest))


def _parents_are_dirs(rootfs, path, what):
    rel = os.path.relpath(os.path.dirname(path), rootfs)
    cur = rootfs
    for comp in rel.split(os.sep):
        if comp == ".":
            continue
        cur = os.path.join(cur, comp)
        if os.path.lexists(cur) and not os.path.isdir(cur):
            raise BrandError("%s parent is not a directory: %s" % (what, cur))


def _reject_tree_symlinks(tree, what):
    for dirpath, dirnames, filenames in os.walk(tree, followlinks=False):
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                raise BrandError("symlink inside %s, refusing: %s" % (what, full))


def _missing_ancestors(home_dir, dst):
    missing = []
    cur = os.path.dirname(dst)
    while cur != home_dir and cur.startswith(home_dir + os.sep):
        if not os.path.lexists(cur):
            missing.append(cur)
        cur = os.path.dirname(cur)
    return missing


def inject_brand(workspace, user=home.LIVE_USER):
    rootfs = _rootfs(workspace)
    src_root = os.path.join(rootfs, BRAND_SOURCE_REL)
    home_dir = os.path.join(rootfs, "home", user)
    src_root = _brand_safe(rootfs, workspace, src_root, "brand source root")
    if not os.path.isdir(src_root):
        raise BrandError("brand source missing in staging: " + src_root)
    if not workspace.contains(home_dir):
        raise BrandError("home path outside workspace: " + home_dir)
    if not os.path.isdir(home_dir):
        raise BrandError("live home missing, run home.build_home first: " + home_dir)
    _audit_source(src_root)
    home_dir = _brand_safe(rootfs, workspace, home_dir, "live home")
    plan = []
    for rel in ALLOWED_SUBPATHS:
        if rel.split(os.sep)[0] in RESERVED_SUBDIRS:
            raise BrandError("reserved payload subdir in allowlist: " + rel)
        src = _brand_safe(rootfs, workspace, os.path.join(src_root, rel),
                          "brand source " + rel)
        if not os.path.lexists(src):
            continue
        if os.path.isdir(src):
            _reject_tree_symlinks(src, "brand source " + rel)
        dst = os.path.join(home_dir, rel)
        dst = _brand_safe(rootfs, workspace, dst, "brand destination " + rel)
        _ensure_inside_home(dst, home_dir, "brand destination " + rel)
        if os.path.isdir(src) and os.path.isdir(dst):
            _reject_tree_symlinks(dst, "existing brand destination " + rel)
        plan.append((rel, src, dst))
    sys_plan = []
    for rel in SYSTEM_BRAND_SUBPATHS:
        src = _brand_safe(rootfs, workspace,
                          os.path.join(src_root, SYSTEM_PAYLOAD_REL, rel),
                          "system brand source " + rel)
        _parents_are_dirs(rootfs, src, "system brand source " + rel)
        if not os.path.lexists(src):
            continue
        if not os.path.isfile(src):
            raise BrandError("system brand source is not a regular file: " + rel)
        dst = _brand_safe(rootfs, workspace, os.path.join(rootfs, rel),
                          "system brand destination " + rel)
        if os.path.lexists(dst) and not os.path.isfile(dst):
            raise BrandError("system brand destination is not a regular file: " + rel)
        _parents_are_dirs(rootfs, dst, "system brand destination " + rel)
        sys_plan.append((rel, src, dst))
    copied = []
    created_dirs = set()
    for rel, src, dst in plan:
        created_dirs.update(_missing_ancestors(home_dir, dst))
        if os.path.isdir(src):
            shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)
        else:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.lexists(dst):
                workspace.remove(dst)
            shutil.copy2(src, dst)
        copied.append(rel)
    if copied:
        home.ensure_live_user(rootfs, user)
        st = os.stat(home_dir)
        uid, gid = st.st_uid, st.st_gid
        try:
            for rel in copied:
                home._chown_tree(os.path.join(home_dir, rel), uid, gid)
            for path in sorted(created_dirs, key=len, reverse=True):
                os.chown(path, uid, gid)
        except OSError as exc:
            raise BrandError("cannot set home-owner ownership on injected brand "
                             "payload: %s" % exc)
    for rel, src, dst in sys_plan:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.lexists(dst):
            workspace.remove(dst)
        shutil.copy2(src, dst)
        try:
            os.chown(dst, 0, 0)
        except OSError as exc:
            raise BrandError("cannot set root:root ownership on %s: %s" % (dst, exc))
    return copied


def assert_required_brand_files(workspace):
    rootfs = _rootfs(workspace)
    for rel in REQUIRED_SYSTEM_BRAND_SUBPATHS:
        path = _brand_safe(rootfs, workspace, os.path.join(rootfs, rel),
                           "required brand file " + rel)
        if os.path.islink(path) or not os.path.isfile(path):
            raise BrandError("required brand file missing or not a regular file: /" + rel)
        st = os.stat(path)
        if st.st_uid != 0 or st.st_gid != 0:
            raise BrandError("required brand file not owned by root:root: /" + rel)
        if stat.S_IMODE(st.st_mode) != 0o644:
            raise BrandError("required brand file has mode %04o, expected 0644: /%s"
                             % (stat.S_IMODE(st.st_mode), rel))
        if os.path.getsize(path) != 0:
            raise BrandError("required brand file is not empty: /" + rel)
    return True