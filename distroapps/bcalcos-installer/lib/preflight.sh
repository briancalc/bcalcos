#!/bin/bash
#PREFLIGHT.SH

# Read-only preflight checks.
#
# This stage deliberately does not modify storage.

REQUIRED_COMMANDS=(
    awk
    blkid
    chroot
    findmnt
    grep
    lsblk
    partprobe
    sgdisk
    mkfs.fat
    mkfs.ext4
    mkswap
    mount
    umount
    unsquashfs
    usermod
    groupmod
    mkpasswd
    grub-install
    grub-mkconfig
    update-initramfs
)

preflight_tools() {
    local missing=()
    local command

    section "Preflight"

    for command in "${REQUIRED_COMMANDS[@]}"; do
        if ! command_exists "$command"; then
            missing+=("$command")
        fi
    done

    if (( ${#missing[@]} > 0 )); then
        error "Missing required commands:"
        printf '    %s\n' "${missing[@]}"
        return 1
    fi

    success "Required preflight commands are available."
    return 0
}

detect_firmware_mode() {
    FIRMWARE_MODE=""

    if [[ -d /sys/firmware/efi ]]; then
        FIRMWARE_MODE="uefi"
    else
        FIRMWARE_MODE="bios"
    fi

    if [[ "$FIRMWARE_MODE" != "uefi" &&
          "$FIRMWARE_MODE" != "bios" ]]; then
        error "Unable to determine firmware mode."
        return 1
    fi

    export FIRMWARE_MODE
    return 0
}
#####################################
# Verify system architecture is x86_64 (required for V1).
# Returns 0 if valid, non-zero otherwise.
verify_architecture() {
    local arch
    arch="$(uname -m)"
    
    if [[ "$arch" != "x86_64" ]]; then
        error "Unsupported architecture: $arch"
        error "BCALCOS Linux V1 requires x86_64."
        return 1
    fi
    
    info "Architecture verified: x86_64"
    return 0
}

locate_snapshot() {
    local candidate

    local candidates=(
        "/run/live/medium/live/filesystem.squashfs"
        "/lib/live/mount/medium/live/filesystem.squashfs"
        "/live/medium/live/filesystem.squashfs"
        "/cdrom/live/filesystem.squashfs"
        "/live/filesystem.squashfs"
    )

    for candidate in "${candidates[@]}"; do
        if [[ -f "$candidate" && -r "$candidate" ]]; then
            SNAPSHOT="$candidate"
            export SNAPSHOT

            success "Located filesystem snapshot: $SNAPSHOT"
            return 0
        fi
    done

    error "filesystem.squashfs was not found."
    error "This is expected when running from the normal build system."
    error "The snapshot will be available when the installer is booted from the live ISO."

    return 1
}

detect_memory() {
    local kib

    kib="$(awk '/^MemTotal:/ {print $2; exit}' /proc/meminfo)"

    if [[ -z "$kib" || ! "$kib" =~ ^[0-9]+$ ]]; then
        error "Unable to determine system RAM."
        return 1
    fi

    RAM_GIB=$(( (kib + 1048575) / 1048576 ))

    if (( RAM_GIB < 1 )); then
        RAM_GIB=1
    fi

    export RAM_GIB

    return 0
}




