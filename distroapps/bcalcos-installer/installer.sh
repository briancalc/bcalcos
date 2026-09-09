#!/bin/bash
# INSTALLER.SH
# BcalcOS Linux Installer
# v1.0
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

INSTALL_USERNAME=""
INSTALL_PASSWORD=""
INSTALL_PASSWORD_CONFIRM=""
INSTALL_HOSTNAME=""
INSTALL_TIMEZONE=""

usage() {
    cat <<EOF
Usage:
    $(basename "$0") --test
    $(basename "$0") --install

Options:
    --test      Run read-only hardware/disk detection and partition planning.
    --install   Run the installation pipeline.
EOF
}

parse_arguments() {
    if (( $# != 1 )); then
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

########### leave at end ###########
main "$@"