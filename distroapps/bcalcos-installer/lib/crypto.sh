#!/bin/bash
#CRYPTO.SH

# BCALCOS disk encryption: LUKS2 home (opt-in), /etc/crypttab
# generation. Swap encryption is boot-time-only via crypttab and
# this file never touches the swap partition.

HOME_CRYPT_NAME="home_crypt"
SWAP_CRYPT_NAME="cryptswap"

###################################
# Collect the home-encryption preference and, if enabled, the LUKS
# passphrase (double entry, silenced).
#
# Sets globals:
#   INSTALL_ENCRYPT_HOME        (1 = yes, 0 = no)
#   INSTALL_LUKS_PASSWORD
#   INSTALL_LUKS_PASSWORD_CONFIRM
collect_home_encryption() {
    INSTALL_ENCRYPT_HOME=0
    INSTALL_LUKS_PASSWORD=""
    INSTALL_LUKS_PASSWORD_CONFIRM=""

    local choice

    printf '\n'
    section "Home Encryption"

    printf '  Encrypt your home partition?\n'
    printf '  Your personal files will require a passphrase each time the computer starts.\n\n'

    printf '    1. Yes\n'
    printf '    2. No\n\n'

    while true; do
        printf '  Choice [1-2]: '

        if ! read -r choice; then
            printf '\n'
            return 1
        fi

        case "$choice" in
            1)
                INSTALL_ENCRYPT_HOME=1
                break
                ;;
            2)
                INSTALL_ENCRYPT_HOME=0
                info "Home encryption disabled."
                return 0
                ;;
            *)
                warning "Invalid choice. Enter 1 or 2."
                ;;
        esac
    done

    printf '\n'
    printf '  Disk encryption passphrase — unlocks your home partition at every boot.\n'
    printf '  This is NOT your login password.\n'
    printf '  If forgotten, your files cannot be recovered.\n'
    printf '  Note: changing your login password later will NOT change this passphrase.\n\n'

    while true; do
        printf '  Passphrase: '

        if ! read -rs INSTALL_LUKS_PASSWORD; then
            printf '\n'
            INSTALL_LUKS_PASSWORD=""
            return 1
        fi

        printf '\n'
        printf '  Confirm passphrase: '

        if ! read -rs INSTALL_LUKS_PASSWORD_CONFIRM; then
            printf '\n'
            INSTALL_LUKS_PASSWORD=""
            INSTALL_LUKS_PASSWORD_CONFIRM=""
            return 1
        fi

        printf '\n'

        if [[ -n "$INSTALL_LUKS_PASSWORD" &&
              "$INSTALL_LUKS_PASSWORD" == "$INSTALL_LUKS_PASSWORD_CONFIRM" ]]; then
            break
        fi

        warning "Passphrase must be non-empty and both entries must match."

        INSTALL_LUKS_PASSWORD=""
        INSTALL_LUKS_PASSWORD_CONFIRM=""
    done

    # Passphrases are deliberately not exported: setup_luks_home()
    # runs in this shell, and the passphrase must stay out of the
    # environment inherited by child processes.
    export INSTALL_ENCRYPT_HOME

    info "Home encryption enabled. LUKS passphrase collected."

    return 0
}

###################################
# Clear the LUKS passphrase variables.
clear_luks_passwords() {
    INSTALL_LUKS_PASSWORD=""
    INSTALL_LUKS_PASSWORD_CONFIRM=""
}

###################################
# Format the home partition as LUKS2 and open it as the home mapper.
# TARGET_HOME_DEVICE is set to the mapper path on success.
setup_luks_home() {
    local home_partition="$1"

    if [[ -z "$home_partition" || ! -b "$home_partition" ]]; then
        error "Invalid home partition: $home_partition"
        return 1
    fi

    if [[ "$home_partition" != "$TARGET_HOME_PARTITION" ]]; then
        error "LUKS setup requested on unexpected device: $home_partition"
        error "Partition plan selected: $TARGET_HOME_PARTITION"
        return 1
    fi

    if [[ -z "$INSTALL_LUKS_PASSWORD" ]]; then
        error "LUKS passphrase was not collected."
        return 1
    fi

    if ! command_exists cryptsetup; then
        error "cryptsetup is required for home encryption."
        return 1
    fi

    info "Creating LUKS2 container on $home_partition..."

    # Passphrase via stdin only. No trailing newline: with
    # --key-file=- cryptsetup uses the input bytes verbatim, so a
    # '\n' would be enrolled into the passphrase and interactive
    # unlock at first boot would fail.
    if ! printf '%s' "$INSTALL_LUKS_PASSWORD" |
        cryptsetup luksFormat \
            --batch-mode \
            --type luks2 \
            --key-file=- \
            "$home_partition"; then
        error "Failed to create LUKS2 container on $home_partition."
        return 1
    fi

    info "Opening LUKS2 container as $HOME_CRYPT_NAME..."

    if ! printf '%s' "$INSTALL_LUKS_PASSWORD" |
        cryptsetup open \
            --key-file=- \
            "$home_partition" "$HOME_CRYPT_NAME"; then
        error "Failed to open LUKS2 container $home_partition."
        return 1
    fi

    local mapper_device="/dev/mapper/$HOME_CRYPT_NAME"

    if [[ ! -b "$mapper_device" ]]; then
        error "Opened mapper device not found: $mapper_device"
        return 1
    fi

    TARGET_HOME_DEVICE="$mapper_device"
    export TARGET_HOME_DEVICE

    success "LUKS2 home container opened: $home_partition -> $TARGET_HOME_DEVICE"

    return 0
}

###################################
# Close the home mapper if it is open. No-op when it does not exist.
# The caller must unmount /home BEFORE calling this function.
close_luks_home() {
    local mapper_device="/dev/mapper/$HOME_CRYPT_NAME"

    if [[ -b "$mapper_device" ]]; then
        info "Closing LUKS container $HOME_CRYPT_NAME..."

        if ! cryptsetup close "$HOME_CRYPT_NAME"; then
            error "Failed to close LUKS container $HOME_CRYPT_NAME."
            return 1
        fi
    fi

    return 0
}

###################################
# Generate /etc/crypttab in the installed system.
# Always: encrypted swap (per-boot random key; 'swap' option means
# cryptdisks runs mkswap on the mapper at each boot). Only when home
# encryption is enabled: the home_crypt entry.
#
# All validation and PARTUUID reads happen BEFORE the output file is
# opened, so a bad argument cannot produce a partially written file.
generate_crypttab() {
    local swap_partition="$1"
    local home_partition="$2"

    local swap_partuuid
    local home_partuuid
    local crypttab_file="$TARGET_ROOT/etc/crypttab"

    if [[ -z "$TARGET_ROOT" || ! -d "$TARGET_ROOT" ]]; then
        error "Target root is not mounted."
        return 1
    fi

    if [[ -z "$swap_partition" || ! -b "$swap_partition" ]]; then
        error "Invalid swap partition: $swap_partition"
        return 1
    fi

    swap_partuuid="$(blkid -s PARTUUID -o value "$swap_partition")" || {
        error "Unable to read swap partition PARTUUID."
        return 1
    }

    if [[ -z "$swap_partuuid" ]]; then
        error "Swap partition PARTUUID is missing."
        return 1
    fi

    if (( ${INSTALL_ENCRYPT_HOME:-0} == 1 )); then
        if [[ -z "$home_partition" || ! -b "$home_partition" ]]; then
            error "Invalid home partition: $home_partition"
            return 1
        fi

        home_partuuid="$(blkid -s PARTUUID -o value "$home_partition")" || {
            error "Unable to read home partition PARTUUID."
            return 1
        }

        if [[ -z "$home_partuuid" ]]; then
            error "Home partition PARTUUID is missing."
            return 1
        fi
    fi

    info "Generating /etc/crypttab..."

    {
        printf '%s\n' '# /etc/crypttab'
        printf '%s\n' '# Generated by BCALCOS Linux Installer.'
        printf '%s\n' '#'
        printf '%s\n' '# Swap is encrypted with a random key on every boot.'
        printf '%s\n' '# There is no passphrase for swap. Hibernation is not supported.'
        printf '\n'

        printf '%s %s %s %s\n' \
            "$SWAP_CRYPT_NAME" \
            "PARTUUID=$swap_partuuid" \
            "/dev/urandom" \
            "swap,cipher=aes-xts-plain64,size=256"

        if (( ${INSTALL_ENCRYPT_HOME:-0} == 1 )); then
            printf '%s %s %s %s\n' \
                "$HOME_CRYPT_NAME" \
                "PARTUUID=$home_partuuid" \
                "none" \
                "luks"
        fi
    } > "$crypttab_file"

    if [[ ! -s "$crypttab_file" ]]; then
        error "Failed to create $crypttab_file."
        return 1
    fi

    success "Generated $TARGET_ROOT/etc/crypttab."

    return 0
}