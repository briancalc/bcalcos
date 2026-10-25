#!/usr/bin/env python3
# GUI/BCALCOS-GUIINSTALLER.PY
# v1.4
#
# BCalcOS Linux Installer - GTK Frontend
#
# This is a FRONTEND only. It never partitions, formats, or touches
# any disk itself. All destructive work is performed by the bash
# engine at /usr/lib/bcalcos-installer/installer.sh.
#
# Engine contract (the --gui/--list-disks/--plan modes are provided
# by installer.sh v1.4):
#
#   installer.sh --list-disks
#       One line per candidate disk:
#           device|size_gib|model
#       Or the single line NO-DISKS when no disk is eligible.
#       The live-media device is already excluded by the engine.
#
#   installer.sh --plan DISK
#       One line describing the partition plan:
#           root=60|swap=8|home=132
#
#   installer.sh --gui
#       Reads one JSON object on stdin (see build_spec()) and runs the
#       full installation pipeline without any interactive prompts.
#       Stdout carries machine-readable progress markers:
#           STAGE <name>     a stage began
#           DONE             installation finished successfully
#           FATAL <message>  installation aborted
#       Anything else is human-readable log text shown in the log view.
#
# Security properties of the handoff:
#   - No user input is passed on any process command line (visible
#     in /proc/*/cmdline) or through the environment.
#   - The whole spec, including the LUKS passphrase, travels once
#     through a pipe on stdin and is not written to disk anywhere.
#   - Secrets necessarily exist in this process's memory (entry
#     fields, Python strings) for as long as the install runs; this
#     is inherent to any installer that collects credentials.
#
# Page flow:
#   1. Input        - all questions, validated as the user types
#   2. Summary      - the full plan; the ONLY confirmation point
#   3. Progress     - stage bar + log; no cancel once started
#   4. Finish       - summary and power-off actions

import json
import os
import re
import subprocess
import sys
import threading

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gdk, GdkPixbuf, Gtk

ENGINE = "/usr/lib/bcalcos-installer/installer.sh"

# Placeholder fill ( GdkPixbuf wants 0xRRGGBBAA )
BRAND_ORANGE = 0xBA7800

# BcalcOS brand palette (colors-and-fonts.txt, LIGHT mode).
# Applied via Gtk.CssProvider to this application's screen. The
# selectors are narrow (window, labels, entries, progressbar) so
# other windows in the live session are not visually affected.
CSS = """
window {
    background-color: #F5F6F8;
    color: #2C3E50;
    font-family: Inter, sans-serif;
}
label.heading {
    font-family: Poppins, sans-serif;
    font-weight: 600;
}
label.hint-error {
    color: #8A3025;
    font-size: 90%;
}
label.farewell-red {
    color: #8A3025;
}
/* Buttons are deliberately styled by the system theme only:
   custom-colored buttons proved fragile against theme label
   colors. Brand color is carried by the progress bar and text
   accents instead. */
progressbar trough {
    background-color: #E2E5E8;
}
progressbar progress {
    background-color: #BA7800;
}
textview {
    font-family: Monospace, monospace;
}
combobox entry, entry {
    background-color: #FBF9F5;
    border-color: #E2E5E8;
}
entry:focus {
    border-color: #E5C88F;
}
checkbutton {
    color: #2C3E50;
}
"""

# Validation rules. These are the SAME rules the engine enforces
# (lib/install.sh transform_installed_user, installer.sh
# collect_hostname). The engine re-validates everything as a
# backstop; the GUI enforces them early so the user can never
# reach the destructive stage with invalid values.
USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
RESERVED_USERNAMES = {"isouser", "root"}
HOSTNAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,13}[a-z0-9]$|^[a-z0-9]$")

# (zone identifier, friendly label). A curated list derived from
# the TUI's original pairs, extended for wider coverage (40 zones
# vs the TUI's 31); the two lists are NOT identical. The label is
# shown in the dropdown; the zone identifier is what reaches the
# engine, which validates it against /usr/share/zoneinfo.
TIMEZONES = [
    ("Pacific/Honolulu", "Honolulu — Hawaii"),
    ("America/Anchorage", "Anchorage — Alaska"),
    ("America/Los_Angeles", "Los Angeles — Pacific Time"),
    ("America/Denver", "Denver — Mountain Time"),
    ("America/Chicago", "Chicago — Central Time"),
    ("America/New_York", "New York — Eastern Time"),
    ("America/Bogota", "Bogota — Colombia"),
    ("America/Halifax", "Halifax — Atlantic Canada"),
    ("America/Sao_Paulo", "Sao Paulo — Brazil"),
    ("America/St_Johns", "St. John's — Newfoundland"),
    ("America/Nuuk", "Nuuk — Greenland"),
    ("Atlantic/Azores", "Azores — Portugal"),
    ("Africa/Cairo", "Cairo — Egypt"),
    ("Africa/Johannesburg", "Johannesburg — South Africa"),
    ("UTC", "UTC"),
    ("Europe/London", "London — United Kingdom"),
    ("Europe/Dublin", "Dublin — Ireland"),
    ("Europe/Paris", "Paris — France"),
    ("Europe/Berlin", "Berlin — Central Europe"),
    ("Europe/Warsaw", "Warsaw — Poland"),
    ("Europe/Helsinki", "Helsinki — Finland"),
    ("Europe/Athens", "Athens — Greece"),
    ("Europe/Bucharest", "Bucharest — Romania"),
    ("Europe/Moscow", "Moscow — Russia"),
    ("Asia/Yekaterinburg", "Yekaterinburg — Russia"),
    ("Asia/Irkutsk", "Irkutsk — Russia"),
    ("Asia/Vladivostok", "Vladivostok — Russia"),
    ("Asia/Dubai", "Dubai — Gulf States"),
    ("Asia/Karachi", "Karachi — Pakistan"),
    ("Asia/Kolkata", "Kolkata — India"),
    ("Asia/Dhaka", "Dhaka — Bangladesh"),
    ("Asia/Bangkok", "Bangkok — Indochina"),
    ("Asia/Jakarta", "Jakarta — Indonesia"),
    ("Asia/Manila", "Manila — Philippines"),
    ("Asia/Shanghai", "Shanghai — China"),
    ("Asia/Tokyo", "Tokyo — Japan/Korea"),
    ("Australia/Sydney", "Sydney — Eastern Australia"),
    ("Pacific/Noumea", "Noumea — New Caledonia"),
    ("Pacific/Auckland", "Auckland — New Zealand"),
    ("Pacific/Apia", "Apia — Samoa"),
]

# Demo mode (BCALCOS_GUI_DEMO=1): feeds fabricated disk and plan
# data so the GUI can be walked page by page with no engine modes
# and no real disks present. Used for styling review and UI tests.
# It NEVER invokes the installer engine: the Install button on the
# progress page runs a scripted fake stage sequence instead.
DEMO_MODE = os.environ.get("BCALCOS_GUI_DEMO", "") == "1"

DEMO_DISKS = [
    ("/dev/sda", "500", "Samsung SSD 870"),
    ("/dev/sdb", "1000", "WD Blue WD10EZEX"),
]

IMAGE_PATHS = [
    "/usr/share/bcalcos-installer/side-input.png",
    "/usr/share/bcalcos-installer/side-summary.png",
    "/usr/share/bcalcos-installer/side-progress.png",
    "/usr/share/bcalcos-installer/side-finish.png",
]


def side_image(index, width, height):
    """Left-side banner. Real art goes in IMAGE_PATHS[index]; until
    then a flat brand-colored placeholder is used so the final layout
    can be judged and the art can be dropped in later without any
    code change."""
    path = IMAGE_PATHS[index]
    if os.path.isfile(path):
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                path, width, height, True)
            return Gtk.Image.new_from_pixbuf(pixbuf)
        except GLib.Error:
            pass
    pixbuf = GdkPixbuf.Pixbuf.new(
        GdkPixbuf.Colorspace.RGB, True, 8, width, height)
    pixbuf.fill(BRAND_ORANGE << 8 | 0xFF)
    return Gtk.Image.new_from_pixbuf(pixbuf)


class InstallerGUI:
    def __init__(self):
        self.disks = []
        self.plan = None
        self.proc = None
        self.farewell_active = False

        self.window = Gtk.Window(title="BcalcOS Installer")
        self.window.set_default_size(860, 560)
        self.window.set_resizable(False)
        # delete-event fires when the user requests the window be
        # closed (X button, alt-F4). Returning True VETOES the close;
        # this is the only GTK3 mechanism that can block it. destroy
        # cannot be vetoed, so it is not used for the guard.
        self.window.connect("delete-event", self.on_window_delete)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT)

        self.build_input_page()
        self.build_summary_page()
        self.build_progress_page()
        self.build_finish_page()

        self.window.add(self.stack)

        style_provider = Gtk.CssProvider()
        style_provider.load_from_data(CSS.encode("utf-8"))
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            style_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        self.window.show_all()

    # ------------------------------------------------------------------
    # Page 1: input
    # ------------------------------------------------------------------
    def build_input_page(self):
        box = Gtk.Box(spacing=18)
        box.pack_start(side_image(0, 220, 560), False, False, 0)

        grid = Gtk.Grid()
        grid.set_row_spacing(8)
        grid.set_column_spacing(12)
        grid.set_margin_top(18)
        grid.set_margin_bottom(18)
        grid.set_margin_end(18)

        row = 0
        grid.attach(Gtk.Label(label="Installation disk:"), 0, row, 1, 1)
        self.disk_combo = Gtk.ComboBoxText()
        self.disk_combo.set_entry_text_column(0)
        self.disk_combo.connect("changed", self.validate_input)
        grid.attach(self.disk_combo, 1, row, 1, 1)

        row += 1
        grid.attach(Gtk.Label(label="Username:"), 0, row, 1, 1)
        self.username_hint = Gtk.Label()
        self.username_hint.get_style_context().add_class("hint-error")
        self.username_hint.set_halign(Gtk.Align.START)
        self.username_entry = Gtk.Entry()
        self.username_entry.set_hexpand(True)
        self.username_entry.connect("changed", self.validate_input)
        self.username_entry.connect(
            "focus-in-event", self.on_field_focused, self.username_hint,
            "1-32 characters: lowercase letters, digits, "
            "hyphens, & underscores.")
        self.username_entry.connect("focus-out-event", self.on_field_blurred)
        grid.attach(self.username_entry, 1, row, 1, 1)
        grid.attach(self.username_hint, 1, row + 1, 1, 1)
        row += 2

        grid.attach(Gtk.Label(label="Password:"), 0, row, 1, 1)
        self.password_entry = Gtk.Entry()
        self.password_entry.set_visibility(False)
        self.password_entry.set_hexpand(True)
        self.password_entry.connect("changed", self.validate_input)
        grid.attach(self.password_entry, 1, row, 1, 1)

        row += 1
        grid.attach(Gtk.Label(label="Confirm:"), 0, row, 1, 1)
        self.password_confirm_entry = Gtk.Entry()
        self.password_confirm_entry.set_visibility(False)
        self.password_confirm_entry.set_hexpand(True)
        self.password_confirm_entry.connect("changed", self.validate_input)
        grid.attach(self.password_confirm_entry, 1, row, 1, 1)
        self.password_hint = Gtk.Label()
        self.password_hint.get_style_context().add_class("hint-error")
        self.password_hint.set_halign(Gtk.Align.START)
        grid.attach(self.password_hint, 1, row + 1, 1, 1)
        row += 2

        grid.attach(Gtk.Label(label="Hostname:"), 0, row, 1, 1)
        self.hostname_hint = Gtk.Label()
        self.hostname_hint.get_style_context().add_class("hint-error")
        self.hostname_hint.set_halign(Gtk.Align.START)
        self.hostname_entry = Gtk.Entry()
        self.hostname_entry.set_placeholder_text("bcalcos")
        self.hostname_entry.set_hexpand(True)
        self.hostname_entry.connect("changed", self.validate_input)
        self.hostname_entry.connect(
            "focus-in-event", self.on_field_focused, self.hostname_hint,
            "1-15 characters: lowercase letters, digits, "
            "& hyphens. empty = bcalcos.")
        self.hostname_entry.connect("focus-out-event", self.on_field_blurred)
        grid.attach(self.hostname_entry, 1, row, 1, 1)
        grid.attach(self.hostname_hint, 1, row + 1, 1, 1)
        row += 2

        grid.attach(Gtk.Label(label="Timezone:"), 0, row, 1, 1)
        self.timezone_combo = Gtk.ComboBoxText()
        for zone, label in TIMEZONES:
            self.timezone_combo.append_text(label)
        self.timezone_combo.set_active(
            next(i for i, (z, _) in enumerate(TIMEZONES) if z == "UTC"))
        grid.attach(self.timezone_combo, 1, row, 1, 1)

        row += 1
        self.encrypt_check = Gtk.CheckButton(
            label="Encrypt my home partition (LUKS2)")
        self.encrypt_check.connect("toggled", self.on_encrypt_toggled)
        grid.attach(self.encrypt_check, 0, row, 2, 1)

        row += 1
        self.luks_box = Gtk.Box(spacing=12)
        self.luks_entry = Gtk.Entry()
        self.luks_entry.set_visibility(False)
        self.luks_entry.set_placeholder_text("Encryption passphrase")
        self.luks_entry.connect("changed", self.validate_input)
        self.luks_confirm_entry = Gtk.Entry()
        self.luks_confirm_entry.set_visibility(False)
        self.luks_confirm_entry.set_placeholder_text("Confirm passphrase")
        self.luks_confirm_entry.connect("changed", self.validate_input)
        self.luks_box.pack_start(self.luks_entry, True, True, 0)
        self.luks_box.pack_start(self.luks_confirm_entry, True, True, 0)
        grid.attach(self.luks_box, 1, row, 1, 1)
        self.luks_hint = Gtk.Label()
        self.luks_hint.get_style_context().add_class("hint-error")
        self.luks_hint.set_halign(Gtk.Align.START)
        grid.attach(self.luks_hint, 1, row + 1, 1, 1)

        self.input_notice = Gtk.Label()
        grid.attach(self.input_notice, 0, 16, 2, 1)

        self.disk_warning = Gtk.Label()
        self.disk_warning.set_markup(
            "<b>Warning:</b>  selected disk will be erased.")
        self.disk_warning.get_style_context().add_class("hint-error")
        self.disk_warning.set_halign(Gtk.Align.START)
        grid.attach(self.disk_warning, 0, 14, 2, 1)

        self.dualboot_note = Gtk.Label()
        self.dualboot_note.set_markup(
            "<b>Note:</b>  dual boot is only supported with two physical disks.")
        self.dualboot_note.get_style_context().add_class("hint-error")
        self.dualboot_note.set_halign(Gtk.Align.START)
        grid.attach(self.dualboot_note, 0, 15, 2, 1)

        self.next_button = Gtk.Button(label="Review installation")
        self.next_button.set_margin_bottom(18)
        self.next_button.set_margin_end(18)
        self.next_button.set_halign(Gtk.Align.END)
        self.next_button.connect("clicked", self.on_next_clicked)

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        page.pack_start(grid, True, True, 0)
        page.pack_end(self.next_button, False, False, 0)
        box.pack_start(page, True, True, 0)

        self.stack.add_titled(box, "input", "Input")
        self.on_encrypt_toggled(self.encrypt_check)

    # ------------------------------------------------------------------
    # Page 2: summary / confirmation
    # ------------------------------------------------------------------
    def build_summary_page(self):
        box = Gtk.Box(spacing=18)
        box.pack_start(side_image(1, 220, 560), False, False, 0)

        self.summary_text = Gtk.Label()
        self.summary_text.set_halign(Gtk.Align.START)
        self.summary_text.get_style_context().add_class("heading")
        self.summary_text.set_valign(Gtk.Align.START)
        self.summary_text.set_margin_top(18)

        warning = Gtk.Label()
        warning.set_markup(
            "<span size=\"larger\"><b>Warning:  the selected disk will "
            "be completely erased.</b></span>\n"
            "All partitions and files on it will be destroyed.\n"
            "This is the point of no return.")
        warning.get_style_context().add_class("hint-error")
        warning.set_halign(Gtk.Align.START)

        button_row = Gtk.Box(spacing=12)
        self.back_button = Gtk.Button(label="Back")
        self.back_button.connect("clicked", self.on_back_clicked)
        self.install_button = Gtk.Button(label="Erase disk and install")
        self.install_button.connect("clicked", self.on_install_clicked)
        button_row.pack_start(self.back_button, False, False, 0)
        button_row.pack_end(self.install_button, False, False, 0)

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        page.pack_start(self.summary_text, True, True, 0)
        page.pack_start(warning, False, False, 0)
        page.pack_end(button_row, False, False, 0)
        page.set_margin_bottom(18)
        page.set_margin_end(18)
        box.pack_start(page, True, True, 0)

        self.stack.add_titled(box, "summary", "Summary")

    # ------------------------------------------------------------------
    # Page 3: progress (no cancel once started)
    # ------------------------------------------------------------------
    def build_progress_page(self):
        box = Gtk.Box(spacing=18)
        box.pack_start(side_image(2, 220, 560), False, False, 0)

        self.stage_label = Gtk.Label(label="Preparing…")
        self.stage_label.set_halign(Gtk.Align.START)
        self.stage_label.get_style_context().add_class("heading")

        self.progress_bar = Gtk.ProgressBar()
        self.progress_bar.set_show_text(True)

        self.log_view = Gtk.TextView()
        self.log_view.set_editable(False)
        self.log_view.set_cursor_visible(False)
        self.log_view.set_monospace(True)
        log_sw = Gtk.ScrolledWindow()
        log_sw.set_min_content_height(160)
        log_sw.add(self.log_view)

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_margin_top(18)
        page.set_margin_bottom(18)
        page.set_margin_end(18)
        page.pack_start(self.stage_label, False, False, 0)
        page.pack_start(self.progress_bar, False, False, 0)
        page.pack_start(log_sw, True, True, 0)
        box.pack_start(page, True, True, 0)

        self.stack.add_titled(box, "progress", "Progress")

    # ------------------------------------------------------------------
    # Page 4: finish
    # ------------------------------------------------------------------
    def build_finish_page(self):
        box = Gtk.Box(spacing=18)
        box.pack_start(side_image(3, 220, 560), False, False, 0)

        self.finish_text = Gtk.Label()
        self.finish_text.set_valign(Gtk.Align.START)
        self.finish_text.get_style_context().add_class("heading")
        self.finish_text.set_margin_top(18)

        self.return_armed = False

        button_row = Gtk.Box(spacing=12)
        button_row.set_margin_bottom(18)
        button_row.set_margin_end(18)
        button_row.set_halign(Gtk.Align.END)
        self.reboot_button = Gtk.Button(label="Restart now")
        self.reboot_button.connect("clicked", self.on_reboot_clicked)
        self.return_button = Gtk.Button(label="Back to Live Session")
        self.return_button.connect("clicked", self.on_return_clicked)
        button_row.pack_start(self.return_button, False, False, 0)
        button_row.pack_end(self.reboot_button, False, False, 0)

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        page.set_margin_end(18)
        page.pack_start(self.finish_text, True, True, 0)
        page.pack_end(button_row, False, False, 0)

        box.pack_start(page, True, True, 0)
        self.stack.add_titled(box, "finish", "Finish")

    # ------------------------------------------------------------------
    # Disk discovery and plan rendering
    # ------------------------------------------------------------------
    def load_disks(self):
        self.disk_combo.remove_all()
        self.disks = []
        if DEMO_MODE:
            for device, size_gib, model in DEMO_DISKS:
                self.disks.append(device)
                self.disk_combo.append_text(
                    "%s  —  %s GiB  —  %s" % (device, size_gib, model))
            if self.disks:
                self.disk_combo.set_active(0)
            return
        try:
            result = subprocess.run(
                [ENGINE, "--list-disks"],
                capture_output=True, text=True, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.input_notice.set_text("Could not list disks: %s" % exc)
            return
        # All-or-nothing: parse every line first, and only commit
        # entries to the selector if the WHOLE listing succeeded. A
        # FATAL line or a non-zero engine exit means the result is
        # untrustworthy, so no partial disk list is ever offered -
        # disk selection is the foundation of every later safety
        # check and must not rest on a failed scan.
        disks = []
        saw_no_disks = False
        fatal = None
        malformed = False
        for line in result.stdout.splitlines():
            if line.strip() == "NO-DISKS":
                saw_no_disks = True
            elif line.startswith("FATAL "):
                fatal = line[6:]
            elif line.count("|") == 2:
                device, size_gib, model = line.split("|")
                # Validate the FIELDS, not just the separator count:
                # a block-device path with a name after /dev/, a
                # positive integer size (the engine floor is 80 GiB,
                # so 0 is impossible in a truthful record), and a
                # non-empty model (disk_model() falls back to
                # "Unknown", never empty). Anything else means the
                # engine's output is not what the contract promises,
                # so the whole listing is rejected.
                if (len(device) > len("/dev/")
                        and device.startswith("/dev/")
                        and size_gib.isdigit() and size_gib != "0"
                        and model):
                    disks.append([device, size_gib, model])
                else:
                    malformed = True
            else:
                # Any line that is neither a marker nor a well-formed
                # device|size|model record means the output is not
                # what the engine contract promises. Trust nothing.
                malformed = True

        # NO-DISKS alongside disk records is contradictory output.
        if saw_no_disks and disks:
            malformed = True

        if (result.returncode != 0 or fatal is not None
                or malformed):
            self.input_notice.set_text(
                "Disk discovery failed: %s" % (fatal or
                    "the installer engine produced unexpected output."
                    if malformed else
                    "the installer engine exited with code %d."
                    % result.returncode))
            return

        if saw_no_disks and not disks:
            self.input_notice.set_text(
                "No eligible installation disks were found. The "
                "minimum disk size is 80 GiB.")
            return

        if not disks:
            self.input_notice.set_text(
                "No installation disks were reported by the engine.")
            return

        for device, size_gib, model in disks:
            self.disks.append(device)
            self.disk_combo.append_text(
                "%s  —  %s GiB  —  %s" % (device, size_gib, model))
        self.disk_combo.set_active(0)

    def selected_disk(self):
        active = self.disk_combo.get_active()
        if active < 0 or active >= len(self.disks):
            return None
        return self.disks[active]

    def refresh_plan(self):
        disk = self.selected_disk()
        self.plan = None
        if disk is None:
            return
        if DEMO_MODE:
            self.plan = {"root": "60", "swap": "8", "home": "412"}
            return
        try:
            result = subprocess.run(
                [ENGINE, "--plan", disk],
                capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return
        if result.returncode != 0:
            # A plan line from a failed engine run is not
            # trustworthy; leave the plan unset so the summary
            # page cannot be reached.
            return
        for line in result.stdout.splitlines():
            if line.startswith("FATAL "):
                # FATAL output alongside a plan line is
                # contradictory; trust neither.
                return
            fields = {}
            for item in line.split("|"):
                if "=" in item:
                    key, value = item.split("=", 1)
                    fields[key] = value
            if {"root", "swap", "home"} <= set(fields):
                self.plan = fields

    # ------------------------------------------------------------------
    # Validation (bug-#1 fix lives here: invalid values can never
    # reach the destructive stage)
    # ------------------------------------------------------------------
    def on_field_focused(self, entry, event, hint, rule):
        """While a rule-bearing field is focused and still empty,
        show its syntax rule as proactive guidance."""
        if not entry.get_text():
            self.set_hint(hint, rule)
        return False

    def on_field_blurred(self, entry, event):
        """On leaving a field, drop guidance so only real errors
        remain visible."""
        self.validate_input()
        return False

    def set_hint(self, label, text):
        """Show a hint label only when it carries text. Empty labels
        are hidden rather than merely blank, so no theme can render
        them as visible artifacts in empty grid rows."""
        label.set_text(text)
        label.set_visible(bool(text))

    def validate_input(self, *args):
        ok = True

        username = self.username_entry.get_text().strip()
        if not username:
            self.set_hint(self.username_hint, "")
            ok = False
        elif username in RESERVED_USERNAMES:
            self.set_hint(self.username_hint, "Reserved name, choose another.")
            ok = False
        elif not USERNAME_RE.match(username):
            self.set_hint(self.username_hint, 
                "1-32 characters: lowercase letters, digits, "
                "hyphens, & underscores.")
            ok = False
        else:
            self.set_hint(self.username_hint, "")

        password = self.password_entry.get_text()
        confirm = self.password_confirm_entry.get_text()
        if not password and not confirm:
            self.set_hint(self.password_hint, "")
            ok = False
        elif password and password == confirm:
            self.set_hint(self.password_hint, "")
        else:
            self.set_hint(self.password_hint, "Passwords must match.")
            ok = False
        if not password:
            ok = False

        hostname = self.hostname_entry.get_text().strip()
        if not hostname:
            hostname = "bcalcos"
        if not HOSTNAME_RE.match(hostname):
            self.set_hint(self.hostname_hint, 
                "1-15 characters: lowercase letters, digits, "
                "& hyphens.")
            ok = False
        else:
            self.set_hint(self.hostname_hint, "")

        if self.encrypt_check.get_active():
            luks = self.luks_entry.get_text()
            luks_confirm = self.luks_confirm_entry.get_text()
            if not luks and not luks_confirm:
                self.set_hint(self.luks_hint, "")
                ok = False
            elif luks and luks == luks_confirm:
                self.set_hint(self.luks_hint, "")
            else:
                self.set_hint(self.luks_hint, "Passphrases must match.")
                ok = False
            if not luks:
                ok = False

        if self.selected_disk() is None:
            ok = False

        self.next_button.set_sensitive(ok)
        return ok

    def on_encrypt_toggled(self, button):
        active = button.get_active()
        for widget in (self.luks_entry, self.luks_confirm_entry):
            widget.set_visible(active)
        self.validate_input()

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------
    def on_next_clicked(self, button):
        if not self.validate_input():
            return
        self.refresh_plan()
        if self.plan is None:
            # A valid plan is mandatory: it proves the engine can
            # compute a layout for this disk, and the summary page
            # displays it. Never proceed to the destructive
            # confirmation without one.
            dialog = Gtk.MessageDialog(
                transient_for=self.window,
                flags=0,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.CLOSE,
                text="Unable to read the partition plan")
            dialog.format_secondary_text(
                "The installer could not compute a partition plan for "
                "the selected disk. Installation cannot proceed.")
            dialog.run()
            dialog.destroy()
            return
        self.populate_summary()
        self.stack.set_visible_child_name("summary")

    def on_back_clicked(self, button):
        self.stack.set_visible_child_name("input")

    def populate_summary(self):
        disk = self.selected_disk()
        hostname = self.hostname_entry.get_text().strip() or "bcalcos"
        encrypt = self.encrypt_check.get_active()
        lines = ["<b>Installation summary</b>", ""]
        lines.append("Disk:       %s" % disk)
        if self.plan:
            lines.append("Root:       %s GiB  (ext4, /)" % self.plan["root"])
            lines.append("Swap:       %s GiB  (encrypted)" % self.plan["swap"])
            lines.append("Home:       %s GiB  (ext4, /home%s)" % (
                self.plan["home"], ", LUKS2" if encrypt else ""))
        else:
            lines.append("Partition plan could not be read.")
        lines.append("Username:   %s" % self.username_entry.get_text().strip())
        lines.append("Hostname:   %s" % hostname)
        zone, label = TIMEZONES[self.timezone_combo.get_active()]
        lines.append("Timezone:   %s (%s)" % (label, zone))
        lines.append("Home encryption: %s" % ("Yes" if encrypt else "No"))
        lines.append("")
        lines.append("")
        lines.append("<small>Swap is encrypted with a per-boot random key. "
                     "Hibernation is not supported.</small>")
        self.summary_text.set_markup("\n".join(lines))

    # ------------------------------------------------------------------
    # Installation (engine contract: JSON spec on stdin, pipe only)
    # ------------------------------------------------------------------
    def build_spec(self):
        hostname = self.hostname_entry.get_text().strip() or "bcalcos"
        spec = {
            "confirmed": True,
            "disk": self.selected_disk(),
            "username": self.username_entry.get_text().strip(),
            "password": self.password_entry.get_text(),
            "hostname": hostname,
            "timezone": TIMEZONES[self.timezone_combo.get_active()][0],
            "encrypt_home": self.encrypt_check.get_active(),
        }
        if spec["encrypt_home"]:
            spec["luks_passphrase"] = self.luks_entry.get_text()
        return spec

    def on_install_clicked(self, button):
        button.set_sensitive(False)
        self.back_button.set_sensitive(False)
        self.stack.set_visible_child_name("progress")
        if DEMO_MODE:
            threading.Thread(target=self.demo_run, daemon=True).start()
            return
        try:
            self.proc = subprocess.Popen(
                [ENGINE, "--gui"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            spec = json.dumps(self.build_spec())
            threading.Thread(
                target=self.feed_stdin, args=(self.proc, spec),
                daemon=True).start()
            threading.Thread(
                target=self.watch_output, args=(self.proc,),
                daemon=True).start()
        except OSError as exc:
            self.install_failed("Could not start engine: %s" % exc)

    def demo_run(self):
        """Fake engine for demo mode: walks the same stage-marker
        sequence the real engine will emit, on the UI thread-safe
        path, so the progress page can be reviewed without risk."""
        import time
        stages = ("Partitioning", "Filesystems", "Mounting",
                  "Extracting snapshot", "Configuring user",
                  "Generating fstab", "Installing GRUB", "Verifying",
                  "Crypttab", "Machine ID", "Timezone", "Hostname",
                  "Admin", "Branding", "GRUB Config", "Cleanup",
                  "Unmount")
        for i, stage in enumerate(stages, start=1):
            GLib.idle_add(self.update_progress, stage, i / len(stages))
            GLib.idle_add(
                self.append_log,
                "demo: %s completed" % stage.lower())
            time.sleep(0.35)
        GLib.idle_add(self.install_done)

    def feed_stdin(self, proc, spec):
        """Runs in a worker thread. The spec - including the LUKS
        passphrase - crosses to the engine exactly once, through the
        pipe, then stdin is closed. Nothing is written to disk."""
        try:
            proc.stdin.write(spec)
            proc.stdin.close()
        except (BrokenPipeError, ValueError):
            pass

    def watch_output(self, proc):
        """Runs in a worker thread. Parses engine stage markers and
        forwards them to the UI thread via idle_add (GTK widgets may
        only be touched from the main thread). The terminal outcome
        is decided HERE, exactly once, and only the outcome callback
        is scheduled on the main thread - a DONE marker cannot race
        a failure dialog."""
        stages_done = 0
        stages_total = 17
        fatal = None
        saw_done = False
        for line in proc.stdout:
            stripped = line.strip()
            if stripped.startswith("STAGE "):
                stage = stripped[6:]
                stages_done += 1
                GLib.idle_add(
                    self.update_progress, stage,
                    min(stages_done / stages_total, 1.0))
            elif stripped.startswith("FATAL "):
                fatal = stripped[6:]
            elif stripped == "DONE":
                saw_done = True
            GLib.idle_add(self.append_log, line.rstrip("\n"))
        proc.wait()

        if saw_done and fatal is None and proc.returncode == 0:
            GLib.idle_add(self.install_done)
        else:
            GLib.idle_add(
                self.install_failed, fatal or
                "Installer exited unexpectedly (code %d)."
                % proc.returncode)

    def append_log(self, text):
        buffer = self.log_view.get_buffer()
        end = buffer.get_end_iter()
        buffer.insert(end, text + "\n")
        return False

    def update_progress(self, stage, fraction):
        self.stage_label.set_text(stage + "…")
        self.progress_bar.set_fraction(fraction)
        return False

    def install_done(self):
        self.populate_finish()
        self.stack.set_visible_child_name("finish")
        return False

    def install_failed(self, message):
        dialog = Gtk.MessageDialog(
            transient_for=self.window,
            flags=0,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE,
            text="Installation failed")
        dialog.format_secondary_text(message)
        dialog.run()
        dialog.destroy()
        self.install_button.set_sensitive(True)
        self.back_button.set_sensitive(True)
        self.stack.set_visible_child_name("input")
        return False

    def populate_finish(self):
        lines = [
            "<span size=\"x-large\"><b>Installation complete.</b></span>",
            "",
            "You may now restart the computer.",
            "Remove the USB drive once the machine is off.",
        ]
        self.finish_text.set_markup("\n".join(lines))
        self.return_armed = False
        self.return_button.set_label("Back to Live Session")

    def on_return_clicked(self, button):
        """Two-step confirmation: the first click arms the button and
        shows the USB warning; the second click within the session
        actually exits to the live desktop. Friction proportional to
        consequence - the user who leaves via this path does so
        deliberately, having read the USB reminder."""
        if not self.return_armed:
            self.return_armed = True
            button.set_label("Really leave? Remove the USB drive before "
                             "this machine restarts. Click again.")
            return
        Gtk.main_quit()

    def on_reboot_clicked(self, button):
        button.set_sensitive(False)
        self.return_button.set_sensitive(False)
        self.show_farewell_overlay()

    def show_farewell_overlay(self):
        """Full-window farewell: unmissable USB instruction plus a
        live 10-second countdown, then sync + reboot. Non-cancellable
        by design (user decision): the Restart click was the last
        decision point."""
        overlay = Gtk.Overlay()
        page = self.stack.get_child_by_name("finish")

        # Detach the finish page from the stack first: a GTK widget
        # cannot have two parents. The stack is then removed from
        # the window and replaced by the overlay, so the farewell
        # covers the whole window.
        self.stack.remove(page)

        farewell = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=18)
        farewell.set_valign(Gtk.Align.CENTER)
        farewell.get_style_context().add_class("farewell")

        title = Gtk.Label()
        title.set_markup(
            "<span size=\"xx-large\" color=\"#8A3025\">"
            "<b>Remove the USB drive now.</b></span>")
        farewell.pack_start(title, False, False, 0)

        subtitle = Gtk.Label()
        subtitle.set_markup(
            "<span size=\"xx-large\">Wait for the restart.</span>")
        farewell.pack_start(subtitle, False, False, 0)

        self.farewell_count_label = Gtk.Label()
        self.farewell_count_label.set_markup(
            "<span size=\"large\">Restarting in 10…</span>")
        farewell.pack_start(self.farewell_count_label, False, False, 0)

        overlay.add(page)
        overlay.add_overlay(farewell)
        farewell.set_halign(Gtk.Align.CENTER)

        self.window.remove(self.stack)
        self.window.add(overlay)
        self.window.show_all()

        self.farewell_active = True
        self.farewell_seconds = 10
        GLib.timeout_add_seconds(1, self.farewell_tick)

    def farewell_tick(self):
        """One-second countdown callback on the main loop."""
        self.farewell_seconds -= 1

        if self.farewell_seconds <= 0:
            self.farewell_count_label.set_text("Restarting…")
            os.sync()
            subprocess.run(["reboot"], check=False)
            return False

        self.farewell_count_label.set_markup(
            "<span size=\"large\">Restarting in %d…</span>"
            % self.farewell_seconds)
        return True

    def on_window_delete(self, widget, event):
        # Refuse to close mid-install: a half-finished install is the
        # worst outcome (user decision). While the engine is running,
        # the request is vetoed and the user stays on the progress
        # page. Also veto during the farewell countdown so the USB
        # instruction remains on screen until the machine restarts.
        if self.farewell_active:
            return True
        if self.proc is not None and self.proc.poll() is None:
            return True
        Gtk.main_quit()
        return False

    # ------------------------------------------------------------------
    def run(self):
        self.load_disks()
        self.validate_input()
        Gtk.main()


def main():
    if os.geteuid() != 0:
        print("This installer must be run as root.", file=sys.stderr)
        sys.exit(1)
    InstallerGUI().run()


if __name__ == "__main__":
    main()