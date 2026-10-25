#!/bin/bash
# INSTALLER.SH
# BcalcOS Linux Installer
# v1.4
# 
# Installation orchestration
# Associated files: 
# common.sh, crypto.sh, disks.sh, install.sh, partition.sh, preflight.sh, ui.sh
#

set -u
set -o pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

source "$SCRIPT_DIR/lib/common.sh"
source "$SCRIPT_DIR/lib/ui.sh"
source "$SCRIPT_DIR/lib/preflight.sh"
source "$SCRIPT_DIR/lib/disks.sh"
source "$SCRIPT_DIR/lib/partition.sh"
source "$SCRIPT_DIR/lib/install.sh"
source "$SCRIPT_DIR/lib/crypto.sh"

TEST_MODE=0

GUI_MODE=0

INSTALL_USERNAME=""
INSTALL_PASSWORD=""
INSTALL_PASSWORD_CONFIRM=""
INSTALL_HOSTNAME=""
INSTALL_TIMEZONE=""

# stage(): emit a machine-readable progress marker in GUI mode.
# The GUI counts STAGE lines against its progress denominator;
# the count of stage() calls in run_installation_gui() must match
# the GUI's stages_total constant.
stage() {
    if (( GUI_MODE == 1 )); then
        printf 'STAGE %s\n' "$1"
    fi
}

gui_fatal() {
    if (( GUI_MODE == 1 )); then
        printf 'FATAL %s\n' "$*"
    fi
    fatal "$@"
}

usage() {
    cat <<EOF
Usage:
    $(basename "$0") --test
    $(basename "$0") --install
    $(basename "$0") --list-disks
    $(basename "$0") --plan DISK
    $(basename "$0") --gui   (reads a JSON spec on stdin)

Options:
    --test        Run read-only hardware/disk detection and partition planning.
    --install     Run the interactive TUI installation pipeline.
    --list-disks  Print candidate disks (device|size_gib|model), one per line.
    --plan DISK   Print the partition plan for DISK (root=X|swap=Y|home=Z).
    --gui         Run the installation pipeline driven by a JSON spec on
                  stdin. Requires "confirmed": true in the spec. No
                  interactive prompts; emits STAGE/FATAL/DONE markers.
EOF
}

parse_arguments() {
    if (( $# < 1 || $# > 2 )); then
        usage
        return 1
    fi

    case "$1" in
        --test)
            TEST_MODE=1
            ;;
        --install)
            TEST_MODE=0
            ;;
        --list-disks)
            GUI_MODE=1
            RUN_MODE="list-disks"
            ;;
        --plan)
            (( $# == 2 )) || { usage; return 1; }
            GUI_MODE=1
            RUN_MODE="plan"
            GUI_PLAN_DISK="$2"
            ;;
        --gui)
            GUI_MODE=1
            RUN_MODE="gui"
            ;;
        --help|-h)
            usage
            return 1
            ;;
        *)
            usage
            return 1
            ;;
    esac

    return 0
}

confirm_installation() {
    printf '\n'
    warning "DESTRUCTIVE OPERATION"
    warning "The selected disk will be completely erased."
    warning "All existing partitions and filesystems on that disk will be destroyed."
    printf '\n'

    printf '  Selected disk: %s\n' "$SELECTED_DISK"
    printf '  Disk size:     %s GiB\n' "$SELECTED_DISK_SIZE_GIB"
    printf '\n'

    printf '  Type the exact device path to confirm: '

    local confirmation

    if ! read -r confirmation; then
        printf '\n'
        return 1
    fi

    if [[ "$confirmation" != "$SELECTED_DISK" ]]; then
        warning "Confirmation did not match the selected disk."
        return 1
    fi

    printf '\n'
    success "Destructive-operation confirmation accepted."

    return 0
}

collect_install_user() {
    printf '\n'
    section "Installed User"

    while true; do
        printf '  Username: '

        if ! read -r INSTALL_USERNAME; then
            printf '\n'
            return 1
        fi

        if [[ -n "$INSTALL_USERNAME" ]]; then
            break
        fi

        warning "Username cannot be empty."
    done

    while true; do
        printf '  Password: '

        if ! read -rs INSTALL_PASSWORD; then
            printf '\n'
            return 1
        fi

        printf '\n'
        printf '  Confirm password: '

        if ! read -rs INSTALL_PASSWORD_CONFIRM; then
            printf '\n'
            return 1
        fi

        printf '\n'

        if [[ "$INSTALL_PASSWORD" == "$INSTALL_PASSWORD_CONFIRM" &&
              -n "$INSTALL_PASSWORD" ]]; then
            break
        fi

        warning "Passwords must be non-empty and must match."
    done

    return 0
}

#########################
collect_hostname() {
    local input

    while true; do
        printf '\n'
        section "Hostname"

        printf '  Enter hostname [bcalcos]: '

        if ! read -r input; then
            printf '\n'
            return 1
        fi

        # Accept default on empty input
        if [[ -z "$input" ]]; then
            input="bcalcos"
        fi

        # Force lowercase
        input="${input,,}"

        # Validate: 1-15 chars, alphanumeric and hyphens only,
        # no leading/trailing hyphen
        if [[ ! "$input" =~ ^[a-z0-9][a-z0-9-]{0,13}[a-z0-9]$|^[a-z0-9]$ ]]; then
            warning "Invalid hostname."
            warning "Use 1-15 characters: lowercase letters, numbers, or hyphens."
            warning "Cannot start or end with a hyphen."
            continue
        fi

        INSTALL_HOSTNAME="$input"
        export INSTALL_HOSTNAME

        info "Hostname: $INSTALL_HOSTNAME"

        return 0
    done
}

###########################
collect_install_timezone() {
    local choice

    local timezones=(
        "Pacific/Honolulu|Hawaii"
        "America/Anchorage|Alaska"
        "America/Los_Angeles|Pacific Time"
        "America/Denver|Mountain Time"
        "America/Chicago|Central Time"
        "America/New_York|Eastern Time"
        "America/Halifax|Atlantic Canada"
        "America/St_Johns|Newfoundland"
        "America/Nuuk|Greenland"
        "Atlantic/Azores|Azores"
        "UTC|UTC"
        "Europe/London|United Kingdom"
        "Europe/Dublin|Ireland"
        "Europe/Paris|France"
        "Europe/Berlin|Central Europe"
        "Europe/Warsaw|Poland"
        "Europe/Helsinki|Finland"
        "Europe/Athens|Greece"
        "Europe/Bucharest|Romania"
        "Europe/Moscow|Russia"
        "Asia/Dubai|Gulf States"
        "Asia/Karachi|Pakistan"
        "Asia/Kolkata|India"
        "Asia/Dhaka|Bangladesh"
        "Asia/Bangkok|Indochina"
        "Asia/Shanghai|China"
        "Asia/Tokyo|Japan/Korea"
        "Australia/Sydney|Eastern Australia"
        "Pacific/Noumea|New Caledonia"
        "Pacific/Auckland|New Zealand"
        "Pacific/Apia|Samoa"
    )

    printf '\n'
    section "Timezone"

    printf '  Select your timezone:\n\n'

    local i=1
    local entry
    local zone
    local name

    for entry in "${timezones[@]}"; do
        zone="${entry%%|*}"
        name="${entry#*|}"

        printf '  %2d. %-22s %s\n' "$i" "$name" "$zone"

        ((i += 1))
    done

    printf '\n'

    while true; do
        printf '  Select timezone [1-%d]: ' "${#timezones[@]}"

        if ! read -r choice; then
            printf '\n'
            return 1
        fi

        if [[ "$choice" =~ ^[0-9]+$ ]] &&
           (( choice >= 1 && choice <= ${#timezones[@]} )); then

            entry="${timezones[$((choice - 1))]}"
            INSTALL_TIMEZONE="${entry%%|*}"

            info "Selected timezone: ${entry#*|}"

            export INSTALL_TIMEZONE

            return 0
        fi

        warning "Invalid selection."
    done
}
####################################
# Exit cleanup handler for fatal errors
# Clears passwords, removes temp files, unmounts target if mounted
cleanup_on_exit() {
    local exit_code=$?

    INSTALL_PASSWORD=""
    INSTALL_PASSWORD_CONFIRM=""
    clear_luks_passwords
    clear_spec_secrets

    if [[ -n "$DISK_IDENTITY_FILE" && -f "$DISK_IDENTITY_FILE" ]]; then
        rm -f -- "$DISK_IDENTITY_FILE" 2>/dev/null || true
    fi

    if [[ -n "$TARGET_ROOT" && -d "$TARGET_ROOT" ]]; then
        umount_target 2>/dev/null || true
    fi

    # If a failed run leaves the home LUKS mapper open (e.g. setup_luks_home
    # succeeded but a later stage aborted), the open mapper holds the home
    # partition busy and every luksFormat retry in this live session fails
    # with "cannot exclusively open, device in use". Close here.
    # umount_target ran first, as required. close_luks_home() is a no-op
    # when the mapper does not exist (unencrypted install, or it never
    # got that far). Never? let cleanup itself fail.
    if [[ -b /dev/mapper/"$HOME_CRYPT_NAME" ]]; then
        if ! close_luks_home; then
            warning "Home mapper could not be closed (still busy?)."
            warning "Reboot the live system before re-running the installer,"
            warning "or close it manually: cryptsetup close $HOME_CRYPT_NAME"
        fi
    fi

    return "$exit_code"
}
####################
run_installation() {
    if ! confirm_installation; then
        fatal "Installation was not confirmed."
    fi

    # Install exit cleanup trap — now that destruction is authorized
    trap cleanup_on_exit EXIT

    if ! collect_install_user; then
        fatal "Unable to collect installed-user credentials."
    fi

    if ! collect_home_encryption; then
        fatal "Unable to collect encryption preferences."
    fi

    if ! collect_hostname; then
        fatal "Unable to collect hostname."
    fi

    if ! collect_install_timezone; then
        fatal "Unable to collect installation timezone."
    fi

    section "Partitioning"

    if ! create_partitions_sgdisk "$SELECTED_DISK"; then
        fatal "Partition creation failed."
    fi

    if ! get_target_partitions "$SELECTED_DISK"; then
        fatal "Unable to determine target partitions."
    fi

    section "Filesystems"

    if ! create_filesystems \
        "$TARGET_ESP" \
        "$TARGET_ROOT_PARTITION" \
        "$TARGET_SWAP" \
        "$TARGET_HOME_PARTITION"; then
        fatal "Filesystem creation failed."
    fi

    clear_luks_passwords

    section "Mount Target"

    if ! mount_target \
        "$TARGET_ROOT_PARTITION" \
        "$TARGET_HOME_DEVICE" \
        "$TARGET_ESP"; then
        fatal "Unable to mount installation target."
    fi

    section "Install Snapshot"

    if ! extract_snapshot "$SNAPSHOT"; then
        error "Snapshot extraction failed."

        if ! unmount_target; then
            error "Target unmount also failed."
        fi

        fatal "Installation aborted."
    fi

    section "Configure Installed User"

    if ! transform_installed_user "$INSTALL_USERNAME"; then
        error "Installed-user transformation failed."

        if ! unmount_target; then
            error "Target unmount also failed."
        fi

        fatal "Installation aborted."
    fi

    if ! set_installed_user_password \
        "$INSTALL_USERNAME" \
        "$INSTALL_PASSWORD" \
        "$INSTALL_PASSWORD_CONFIRM"; then
        error "Installed-user password configuration failed."

        if ! unmount_target; then
            error "Target unmount also failed."
        fi

        fatal "Installation aborted."
    fi

    section "Configure Admin User"

    if ! configure_admin_user "$INSTALL_USERNAME"; then
        error "Admin user configuration failed."

        if ! unmount_target; then
            error "Target unmount also failed."
        fi

        fatal "Installation aborted."
    fi

    section "Configure Timezone"

    if ! set_install_timezone "$INSTALL_TIMEZONE"; then
        error "Timezone configuration failed."

        if ! unmount_target; then
            error "Target unmount also failed."
        fi

        fatal "Installation aborted."
    fi

    section "Configure Hostname"

    if ! generate_hostname "$INSTALL_HOSTNAME"; then
        error "Hostname configuration failed."

        if ! unmount_target; then
            error "Target unmount also failed."
        fi

        fatal "Installation aborted."
    fi

    section "Generate Machine ID"

    if ! generate_machine_id; then
        error "Machine-id generation failed."

        if ! unmount_target; then
            error "Target unmount also failed."
        fi

        fatal "Installation aborted."
    fi

    section "Generate fstab"

    if ! generate_fstab \
        "$TARGET_ESP" \
        "$TARGET_ROOT_PARTITION" \
        "$TARGET_SWAP" \
        "$TARGET_HOME_DEVICE"; then
        error "fstab generation failed."

        if ! unmount_target; then
            error "Target unmount also failed."
        fi

        fatal "Installation aborted."
    fi

    section "Generate crypttab"

    if ! generate_crypttab \
        "$TARGET_SWAP" \
        "$TARGET_HOME_PARTITION"; then
        error "crypttab generation failed."

        if ! unmount_target; then
            error "Target unmount also failed."
        fi

        fatal "Installation aborted."
    fi

    section "Install Bootloader"

    if [[ "$FIRMWARE_MODE" == "uefi" ]]; then
        if ! install_grub_uefi "$SELECTED_DISK"; then
            error "UEFI GRUB installation failed."

            if ! unmount_target; then
                error "Target unmount also failed."
            fi

            fatal "Installation aborted."
        fi

        if ! write_grub_stubs; then
            error "GRUB stub configuration failed."

            if ! unmount_target; then
                error "Target unmount also failed."
            fi

            fatal "Installation aborted."
        fi

    elif [[ "$FIRMWARE_MODE" == "bios" ]]; then
        if ! install_grub_bios "$SELECTED_DISK"; then
            error "BIOS GRUB installation failed."

            if ! unmount_target; then
                error "Target unmount also failed."
            fi

            fatal "Installation aborted."
        fi
    else
        error "Unknown firmware mode: $FIRMWARE_MODE"

        if ! unmount_target; then
            error "Target unmount also failed."
        fi

        fatal "Installation aborted."
    fi

    section "Install GRUB Branding"

    if ! install_grub_branding "$SNAPSHOT"; then
        error "GRUB branding installation failed."

        if ! unmount_target; then
            error "Target unmount also failed."
        fi

        fatal "Installation aborted."
    fi

    section "Cleanup Live-Boot Components"

    if ! cleanup_live_boot; then
        error "Live-boot cleanup failed."

        if ! unmount_target; then
            error "Target unmount also failed."
        fi

        fatal "Installation aborted."
    fi

    section "Generate GRUB Configuration"

    if ! generate_grub_config; then
        error "GRUB configuration generation failed."

        if ! unmount_target; then
            error "Target unmount also failed."
        fi

        fatal "Installation aborted."
    fi

    section "Cleanup"

    if ! cleanup; then
        error "Cleanup failed."

        if ! unmount_target; then
            error "Target unmount also failed."
        fi

        fatal "Installation aborted."
    fi

    if ! verify_installation "$INSTALL_USERNAME"; then
        error "Installation verification failed."

        if ! unmount_target; then
            error "Target unmount also failed."
        fi

        fatal "Installation verification failed."
    fi

    section "Unmount Target"

    if ! unmount_target; then
        fatal "Installation completed but target unmount failed."
    fi

    INSTALL_PASSWORD=""
    INSTALL_PASSWORD_CONFIRM=""
    clear_luks_passwords

    rm -f "$DISK_IDENTITY_FILE" 2>/dev/null || true

    show_final_confirmation

    show_shutdown_instructions

    printf '  Power off now? [Y/n]: '

    local poweroff_choice
    if ! read -r poweroff_choice; then
        printf '\n'
        return 0
    fi

    if [[ "$poweroff_choice" =~ ^[Yy]$ || -z "$poweroff_choice" ]]; then
        info "Syncing filesystems..."
        sync
        info "Powering off in 5 seconds. Remove the USB once the machine is off."
        sleep 5
        poweroff
    else
        info "Installation complete. You may power off manually when ready."
    fi

    return 0
}

main() {
    parse_arguments "$@" || return 1

    if [[ "${RUN_MODE:-}" == "list-disks" ]]; then
        require_root || gui_fatal "This stage must be run as root."
        detect_live_media_device || gui_fatal "Unable to identify the live installer media."
        preflight_tools >/dev/null 2>&1 || gui_fatal "Required preflight tools are missing."
        run_gui_list_disks
        return $?
    fi

    if [[ "${RUN_MODE:-}" == "plan" ]]; then
        require_root || gui_fatal "This stage must be run as root."
        detect_live_media_device || gui_fatal "Unable to identify the live installer media."
        preflight_tools >/dev/null 2>&1 || gui_fatal "Required preflight tools are missing."
        detect_firmware_mode || gui_fatal "Unable to determine firmware mode."
        run_gui_plan "$GUI_PLAN_DISK"
        return $?
    fi

    if [[ "${RUN_MODE:-}" == "gui" ]]; then
        require_root || gui_fatal "This stage must be run as root."
        preflight_tools || gui_fatal "Required preflight tools are missing."
        detect_firmware_mode || gui_fatal "Unable to determine firmware mode."
        verify_architecture || gui_fatal "Architecture verification failed."

        if ! detect_live_media_device; then
            gui_fatal "Unable to identify the live installer media."
        fi

        if ! locate_snapshot; then
            gui_fatal "Unable to locate live/filesystem.squashfs."
        fi

        if ! detect_memory; then
            gui_fatal "Unable to determine system RAM."
        fi

        run_installation_gui
        return $?
    fi

    ui_init
    show_banner

    if ! require_root; then
        fatal "This development stage must be run as root."
    fi

    if ! preflight_tools; then
        fatal "Required preflight tools are missing."
    fi

    if ! detect_firmware_mode; then
        fatal "Unable to determine firmware mode."
    fi

    if ! verify_architecture; then
        fatal "Architecture verification failed."
    fi

    if detect_live_media_device; then
        info "Live installer media detected on: $LIVE_MEDIA_DEVICE"
    else
        fatal "Unable to identify the live installer media. Installation cannot safely continue."
    fi

    if (( TEST_MODE == 0 )); then
        if ! locate_snapshot; then
            fatal "Unable to locate live/filesystem.squashfs."
        fi

        show_snapshot
    else
        warning "READ-ONLY TEST MODE"
        warning "No snapshot extraction or disk modification is possible."
    fi

    if ! detect_memory; then
        fatal "Unable to determine system RAM."
    fi

    show_firmware
    show_memory

    if ! select_installation_disk; then
        fatal "No suitable installation disk was selected."
    fi

    if ! calculate_partition_plan "$SELECTED_DISK_SIZE_GIB"; then
        fatal "Unable to calculate a valid partition layout."
    fi

    show_partition_plan

    if (( TEST_MODE == 1 )); then
        printf '\n'
        info "THIS VERSION DOES NOT MODIFY ANY DISK."
        success "Read-only preflight and partition planning completed."
        return 0
    fi

    run_installation

    return 0
}

#####################################
# GUI MODE SUPPORT (v1.4)
#
# The GUI frontend (gui/bcalcos-guiinstaller.py) is a pure
# frontend: it never touches disks. It calls this engine as:
#
#   installer.sh --list-disks    -> device|size_gib|model lines
#   installer.sh --plan DISK     -> root=X|swap=Y|home=Z
#   installer.sh --gui           -> full pipeline, JSON spec on stdin
#
# Security contract for --gui:
#   - the spec (including passwords and the LUKS passphrase)
#     arrives on stdin and is never written to disk
#   - no user input is passed on any process command line or in
#     the environment; values live in shell variables in this
#     process's memory for the duration of the install
#   - the engine re-validates EVERYTHING, exactly like the TUI;
#     GUI-side validation is convenience, engine-side is the
#     guarantee
#   - the spec must contain "confirmed": true or the run aborts
#     before anything destructive happens

# Read the JSON spec from stdin, extract fields via an embedded
# python3 helper (python3 is already a hard dependency of the GUI),
# and import plain shell variables. Input is capped at 64 KiB:
# a valid spec is ~300 bytes, anything larger is malformed.
#
# The python helper emits every value base64-encoded, and the
# values are decoded below WITHOUT eval: an unencoded value
# containing spaces, $, or command substitutions would either
# break shell word-splitting or be EXECUTED by eval. Base64 is
# eval-proof by construction; no eval is used at all.
read_gui_spec() {
    local spec_json
    spec_json="$(head -c 65536)" || {
        gui_fatal "Unable to read the JSON spec from stdin."
        return 1
    }

    if [[ -z "$spec_json" ]]; then
        gui_fatal "No JSON spec received on stdin."
        return 1
    fi

    # The JSON travels on stdin only: passing it as a python
    # argument would expose credentials in /proc/<pid>/cmdline.
    # The -c program text is static and contains no secrets.
    local extracted
    local py_rc
    extracted="$(printf '%s' "$spec_json" | python3 -c '
import base64, json, sys

raw = sys.stdin.read()
try:
    spec = json.loads(raw)
except ValueError:
    sys.exit(2)

if not isinstance(spec, dict):
    sys.exit(2)

def emit(key, value):
    if isinstance(value, bool):
        value = "1" if value else "0"
    elif not isinstance(value, str):
        value = str(value)
    encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
    sys.stdout.write("%s=%s\n" % (key, encoded))

emit("SPEC_CONFIRMED", spec.get("confirmed", ""))
emit("SPEC_DISK", spec.get("disk", ""))
emit("SPEC_USERNAME", spec.get("username", ""))
emit("SPEC_PASSWORD", spec.get("password", ""))
emit("SPEC_HOSTNAME", spec.get("hostname", ""))
emit("SPEC_TIMEZONE", spec.get("timezone", ""))
emit("SPEC_ENCRYPT_HOME", spec.get("encrypt_home", ""))
emit("SPEC_LUKS_PASSPHRASE", spec.get("luks_passphrase", ""))
' 2>/dev/null)" ; py_rc=$?

    if (( py_rc == 2 )); then
        gui_fatal "The JSON spec is malformed."
        return 1
    elif (( py_rc != 0 )); then
        gui_fatal "Unable to parse the JSON spec (python3 missing?)."
        return 1
    fi

    # Import the extracted values WITHOUT eval: each line is
    # name=base64, split by hand and decoded with base64 -d.
    local line key encoded
    while IFS='=' read -r key encoded; do
        case "$key" in
            SPEC_CONFIRMED|SPEC_DISK|SPEC_USERNAME|SPEC_PASSWORD|\
            SPEC_HOSTNAME|SPEC_TIMEZONE|SPEC_ENCRYPT_HOME|\
            SPEC_LUKS_PASSPHRASE)
                ;;
            *)
                continue
                ;;
        esac
        printf -v "$key" '%s' "$(printf '%s' "$encoded" | base64 -d)"
    done <<<"$extracted"

    # The raw JSON buffer is dropped here; the shell variables are
    # the only copy. cleanup_on_exit clears the secret ones.
    spec_json=""
    extracted=""

    return 0
}

# Re-validate every collected value, exactly as the TUI versions do.
# Any failure aborts BEFORE partitioning, so an invalid spec can
# never cost the user their disk.
validate_gui_spec() {
    if [[ "$SPEC_CONFIRMED" != "1" ]]; then
        gui_fatal "The spec is missing confirmed=true. Aborting."
        return 1
    fi

    if [[ -z "$SPEC_DISK" || ! -b "$SPEC_DISK" ]]; then
        gui_fatal "Invalid disk in spec: $SPEC_DISK"
        return 1
    fi

    if ! disk_is_eligible "$SPEC_DISK"; then
        gui_fatal "Disk is not an eligible installation target: $SPEC_DISK"
        return 1
    fi

    if [[ -z "$SPEC_USERNAME" ]]; then
        gui_fatal "Username is empty."
        return 1
    fi

    # Same regex the TUI engine enforces in transform_installed_user().
    if [[ ! "$SPEC_USERNAME" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]]; then
        gui_fatal "Invalid username in spec: $SPEC_USERNAME"
        return 1
    fi

    if [[ "$SPEC_USERNAME" == "isouser" || "$SPEC_USERNAME" == "root" ]]; then
        gui_fatal "Reserved username in spec: $SPEC_USERNAME"
        return 1
    fi

    if [[ -z "$SPEC_PASSWORD" ]]; then
        gui_fatal "Password is empty."
        return 1
    fi

    if [[ -z "$SPEC_HOSTNAME" ]]; then
        SPEC_HOSTNAME="bcalcos"
    fi

    if [[ ! "$SPEC_HOSTNAME" =~ ^[a-z0-9][a-z0-9-]{0,13}[a-z0-9]$|^[a-z0-9]$ ]]; then
        gui_fatal "Invalid hostname in spec: $SPEC_HOSTNAME"
        return 1
    fi

    if [[ -z "$SPEC_TIMEZONE" ]]; then
        gui_fatal "Timezone is empty."
        return 1
    fi

    # Area/Location zones contain a slash; UTC is the one legal
    # slash-free zone identifier this installer offers.
    if [[ ! "$SPEC_TIMEZONE" =~ ^[A-Za-z0-9_+/-]+/[A-Za-z0-9_+/-]+$ ]] &&
       [[ "$SPEC_TIMEZONE" != "UTC" ]]; then
        gui_fatal "Invalid timezone format in spec: $SPEC_TIMEZONE"
        return 1
    fi

    # Existence check: the format regex proves shape only. Confirm
    # the zone exists in the live system's zoneinfo before any
    # destructive work (set_install_timezone re-checks against the
    # installed tree at configure time; this catches a bad zone
    # BEFORE the disk is wiped, when the user can still walk away).
    if [[ "$SPEC_TIMEZONE" != "UTC" &&
          ! -f "/usr/share/zoneinfo/$SPEC_TIMEZONE" ]]; then
        gui_fatal "Timezone is not present in the live system's tzdata: $SPEC_TIMEZONE"
        return 1
    fi

    if [[ "$SPEC_ENCRYPT_HOME" != "0" && "$SPEC_ENCRYPT_HOME" != "1" ]]; then
        gui_fatal "Invalid encrypt_home value in spec."
        return 1
    fi

    if [[ "$SPEC_ENCRYPT_HOME" == "1" && -z "$SPEC_LUKS_PASSPHRASE" ]]; then
        gui_fatal "Home encryption requested but the LUKS passphrase is empty."
        return 1
    fi

    return 0
}

# Map the spec values onto the same globals the pipeline uses.
apply_gui_spec() {
    INSTALL_USERNAME="$SPEC_USERNAME"
    INSTALL_PASSWORD="$SPEC_PASSWORD"
    INSTALL_PASSWORD_CONFIRM="$SPEC_PASSWORD"
    INSTALL_HOSTNAME="$SPEC_HOSTNAME"
    INSTALL_TIMEZONE="$SPEC_TIMEZONE"
    INSTALL_ENCRYPT_HOME="$SPEC_ENCRYPT_HOME"
    INSTALL_LUKS_PASSWORD="$SPEC_LUKS_PASSPHRASE"
    INSTALL_LUKS_PASSWORD_CONFIRM="$SPEC_LUKS_PASSPHRASE"
    export INSTALL_ENCRYPT_HOME
    export INSTALL_HOSTNAME
    export INSTALL_TIMEZONE
}

# --list-disks: print the candidate disks, one per line. An empty
# but successful listing (no eligible disks) prints nothing and
# exits 0; any discovery failure prints FATAL and exits 1, so the
# GUI can distinguish "no candidates" from "could not scan".
# Discovery is all-or-nothing: candidates are captured FIRST and a
# failure of get_candidate_disks aborts the whole listing. The
# earlier while-read < <(...) form could not observe that command's
# exit status (process substitution does not propagate it), so a
# failed scan was indistinguishable from an empty result.
run_gui_list_disks() {
    local candidates
    local scan_rc

    candidates="$(get_candidate_disks)" ; scan_rc=$?

    if (( scan_rc != 0 )); then
        gui_fatal "Candidate disk discovery failed."
        return 1
    fi

    local disk
    local size
    local model
    local found=0

    while IFS= read -r disk; do
        [[ -n "$disk" ]] || continue
        size="$(disk_size_gib "$disk")" || {
            gui_fatal "Unable to determine size of $disk"
            return 1
        }
        model="$(disk_model "$disk")" || model="Unknown"
        printf '%s|%s|%s\n' "$disk" "$size" "$model"
        found=1
    done <<<"$candidates"

    if (( found == 0 )); then
        printf 'NO-DISKS\n'
    fi

    return 0
}

# --plan DISK: print the partition plan for one disk.
run_gui_plan() {
    local disk="$1"

    if [[ -z "$disk" || ! -b "$disk" ]]; then
        gui_fatal "Invalid disk: $disk"
        return 1
    fi

    if ! disk_is_eligible "$disk"; then
        gui_fatal "Disk is not an eligible installation target: $disk"
        return 1
    fi

    if ! detect_memory; then
        gui_fatal "Unable to determine system RAM."
        return 1
    fi

    SELECTED_DISK="$disk"
    SELECTED_DISK_SIZE_GIB="$(disk_size_gib "$disk")" || {
        gui_fatal "Unable to determine disk size: $disk"
        return 1
    }

    if ! calculate_partition_plan "$SELECTED_DISK_SIZE_GIB"; then
        gui_fatal "Unable to calculate a partition plan for $disk"
        return 1
    fi

    printf 'root=%s|swap=%s|home=%s\n' \
        "$ROOT_SIZE_GIB" "$SWAP_SIZE_GIB" "$HOME_SIZE_GIB"

    return 0
}

# --gui: the full pipeline driven by the spec. Mirrors run_installation
# stage for stage, but with collection replaced by the spec and all
# interactive prompts removed. Ends with DONE; the GUI (not the
# engine) owns the power-off decision.
run_installation_gui() {
    read_gui_spec || return 1
    validate_gui_spec || return 1

    # Apply the validated spec BEFORE any destructive operation:
    # create_filesystems() reads INSTALL_ENCRYPT_HOME and the LUKS
    # passphrase while creating the home filesystem, so these
    # globals must already hold the user's choices by then. The
    # passphrase is cleared by create_filesystems/clear_luks_passwords
    # and by cleanup_on_exit as in the TUI flow.
    apply_gui_spec

    # Install exit cleanup trap - destruction is authorized by
    # confirmed=true in the spec.
    trap cleanup_on_exit EXIT

    SELECTED_DISK="$SPEC_DISK"
    SELECTED_DISK_SIZE_GIB="$(disk_size_gib "$SELECTED_DISK")" || {
        gui_fatal "Unable to determine disk size."
        return 1
    }

    # Calculate the partition plan. The TUI does this in main()
    # after disk selection; the GUI path never runs that code, so
    # the plan must be calculated here or create_partitions_sgdisk()
    # receives the common.sh initializers (all zero) and sgdisk
    # fails with degenerate sizes.
    if ! calculate_partition_plan "$SELECTED_DISK_SIZE_GIB"; then
        gui_fatal "Unable to calculate a valid partition layout."
        return 1
    fi

    # Record the disk identity for TOCTOU verification. The TUI
    # does this inside select_installation_disk(); the GUI replaces
    # selection with the spec's disk, so the identity must be
    # recorded here or create_partitions_sgdisk() refuses to work
    # (disk_identity_check fatals with "No disk identity found").
    # Same mechanism, same fields, as the TUI path.
    DISK_IDENTITY_FILE="$(mktemp /tmp/bcalcos-disk-identity.XXXXXX)" || {
        gui_fatal "Unable to create disk identity record."
        return 1
    }
    chmod 600 "$DISK_IDENTITY_FILE"

    disk_serial=""
    disk_wwn=""
    disk_serial="$(lsblk -dnro SERIAL "$SELECTED_DISK" 2>/dev/null)" || disk_serial=""
    disk_wwn="$(lsblk -dnro WWN "$SELECTED_DISK" 2>/dev/null)" || disk_wwn=""

    printf 'size=%s\nmodel=%s\nserial=%s\nwwn=%s\n' \
        "$(disk_size_bytes "$SELECTED_DISK")" \
        "$(disk_model "$SELECTED_DISK")" \
        "$disk_serial" \
        "$disk_wwn" > "$DISK_IDENTITY_FILE"

    export DISK_IDENTITY_FILE

    stage "Partitioning"
    section "Partitioning"
    if ! create_partitions_sgdisk "$SELECTED_DISK"; then
        gui_fatal "Partition creation failed."
        return 1
    fi

    if ! get_target_partitions "$SELECTED_DISK"; then
        gui_fatal "Unable to determine target partitions."
        return 1
    fi

    stage "Filesystems"
    section "Filesystems"
    if ! create_filesystems \
        "$TARGET_ESP" \
        "$TARGET_ROOT_PARTITION" \
        "$TARGET_SWAP" \
        "$TARGET_HOME_PARTITION"; then
        gui_fatal "Filesystem creation failed."
        return 1
    fi

    clear_luks_passwords

    stage "Mount Target"
    section "Mount Target"
    if ! mount_target \
        "$TARGET_ROOT_PARTITION" \
        "$TARGET_HOME_DEVICE" \
        "$TARGET_ESP"; then
        gui_fatal "Unable to mount installation target."
        return 1
    fi

    stage "Install Snapshot"
    section "Install Snapshot"
    if ! extract_snapshot "$SNAPSHOT"; then
        gui_fatal "Snapshot extraction failed."
        return 1
    fi

    stage "Configure User"
    section "Configure Installed User"
    if ! transform_installed_user "$INSTALL_USERNAME"; then
        gui_fatal "Installed-user transformation failed."
        return 1
    fi

    if ! set_installed_user_password \
        "$INSTALL_USERNAME" \
        "$INSTALL_PASSWORD" \
        "$INSTALL_PASSWORD_CONFIRM"; then
        gui_fatal "Installed-user password configuration failed."
        return 1
    fi

    stage "Configure Admin"
    section "Configure Admin User"
    if ! configure_admin_user "$INSTALL_USERNAME"; then
        gui_fatal "Admin user configuration failed."
        return 1
    fi

    stage "Configure Timezone"
    section "Configure Timezone"
    if ! set_install_timezone "$INSTALL_TIMEZONE"; then
        gui_fatal "Timezone configuration failed."
        return 1
    fi

    stage "Configure Hostname"
    section "Configure Hostname"
    if ! generate_hostname "$INSTALL_HOSTNAME"; then
        gui_fatal "Hostname configuration failed."
        return 1
    fi

    stage "Machine ID"
    section "Generate Machine ID"
    if ! generate_machine_id; then
        gui_fatal "Machine-id generation failed."
        return 1
    fi

    stage "fstab"
    section "Generate fstab"
    if ! generate_fstab \
        "$TARGET_ESP" \
        "$TARGET_ROOT_PARTITION" \
        "$TARGET_SWAP" \
        "$TARGET_HOME_DEVICE"; then
        gui_fatal "fstab generation failed."
        return 1
    fi

    stage "crypttab"
    section "Generate crypttab"
    if ! generate_crypttab \
        "$TARGET_SWAP" \
        "$TARGET_HOME_PARTITION"; then
        gui_fatal "crypttab generation failed."
        return 1
    fi

    stage "Bootloader"
    section "Install Bootloader"
    if [[ "$FIRMWARE_MODE" == "uefi" ]]; then
        if ! install_grub_uefi "$SELECTED_DISK"; then
            gui_fatal "UEFI GRUB installation failed."
            return 1
        fi

        if ! write_grub_stubs; then
            gui_fatal "GRUB stub configuration failed."
            return 1
        fi
    elif [[ "$FIRMWARE_MODE" == "bios" ]]; then
        if ! install_grub_bios "$SELECTED_DISK"; then
            gui_fatal "BIOS GRUB installation failed."
            return 1
        fi
    else
        gui_fatal "Unknown firmware mode: $FIRMWARE_MODE"
        return 1
    fi

    stage "Branding"
    section "Install GRUB Branding"
    if ! install_grub_branding "$SNAPSHOT"; then
        gui_fatal "GRUB branding installation failed."
        return 1
    fi

    stage "Cleanup"
    section "Cleanup Live-Boot Components"
    if ! cleanup_live_boot; then
        gui_fatal "Live-boot cleanup failed."
        return 1
    fi

    stage "GRUB Config"
    section "Generate GRUB Configuration"
    if ! generate_grub_config; then
        gui_fatal "GRUB configuration generation failed."
        return 1
    fi

    stage "Final Cleanup"
    section "Cleanup"
    if ! cleanup; then
        gui_fatal "Cleanup failed."
        return 1
    fi

    if ! verify_installation "$INSTALL_USERNAME"; then
        gui_fatal "Installation verification failed."
        return 1
    fi

    stage "Unmount"
    section "Unmount Target"
    if ! unmount_target; then
        gui_fatal "Target unmount failed after a completed installation."
        return 1
    fi

    INSTALL_PASSWORD=""
    INSTALL_PASSWORD_CONFIRM=""
    clear_luks_passwords
    clear_spec_secrets

    rm -f "$DISK_IDENTITY_FILE" 2>/dev/null || true

    printf 'DONE\n'

    return 0
}

# Clear the SPEC_* credential copies produced by read_gui_spec.
# These are parallel copies of the same secrets that live in the
# INSTALL_* variables; leaving them populated would mean two sets
# of variables to keep track of. (Assignment empties the shell
# variables; it does not guarantee the old bytes are wiped from
# memory - the comments here claim no more than that.) These are
# NOT exported, so they never existed in the environment.
clear_spec_secrets() {
    SPEC_PASSWORD=""
    SPEC_LUKS_PASSPHRASE=""
}

########### leave at end ###########
main "$@"