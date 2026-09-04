#!/bin/bash
#PARTITION.SH

# Deterministic V1 partition calculator.
#
# UEFI:
#   1 GiB EFI System Partition
#
# Legacy BIOS:
#   2 MiB BIOS Boot Partition
#
# Both:
#   root
#   swap
#   home
#
# Root:
#   80 GiB  -> 20 GiB
#   200 GiB -> 60 GiB
#   linear between those values
#   rounded to nearest whole GiB
#   >=200 GiB -> 60 GiB
#
# Swap:
#   RAM GiB
#   minimum 2 GiB
#
# Home:
#   all remaining space
#   minimum 10 GiB

MIN_ROOT_GIB=20
MAX_ROOT_GIB=60
MIN_HOME_GIB=10
MIN_SWAP_GIB=2

###################################
# Returns the device path for partition N on the given disk.
# Handles NVMe (nvme0n1p1), MMC (mmcblk0p1), and SD (sda1) naming.
get_partition_path() {
    local disk="$1"
    local partition_number="$2"

    if [[ "$disk" =~ [0-9]$ ]]; then
        printf '%sp%d\n' "$disk" "$partition_number"
    else
        printf '%s%d\n' "$disk" "$partition_number"
    fi
}

###################################
# Populate TARGET_* partition path globals for the selected disk.
# Uses get_partition_path() for correct device naming.
get_target_partitions() {
    local disk="$1"

    TARGET_ESP=""
    TARGET_ROOT_PARTITION=""
    TARGET_SWAP=""
    TARGET_HOME_PARTITION=""

    if [[ "$FIRMWARE_MODE" == "uefi" ]]; then
        TARGET_ESP="$(get_partition_path "$disk" 1)"
        TARGET_ROOT_PARTITION="$(get_partition_path "$disk" 2)"
        TARGET_SWAP="$(get_partition_path "$disk" 3)"
        TARGET_HOME_PARTITION="$(get_partition_path "$disk" 4)"
    elif [[ "$FIRMWARE_MODE" == "bios" ]]; then
        TARGET_ESP=""
        TARGET_ROOT_PARTITION="$(get_partition_path "$disk" 2)"
        TARGET_SWAP="$(get_partition_path "$disk" 3)"
        TARGET_HOME_PARTITION="$(get_partition_path "$disk" 4)"
    else
        error "Unknown firmware mode: $FIRMWARE_MODE"
        return 1
    fi

    export TARGET_ESP
    export TARGET_ROOT_PARTITION
    export TARGET_SWAP
    export TARGET_HOME_PARTITION

    return 0
}

calculate_root_size() {
    local disk_gib="$1"
    local root

    if [[ ! "$disk_gib" =~ ^[0-9]+$ ]]; then
        return 1
    fi

    if (( disk_gib < 80 )); then
        return 1
    fi

    if (( disk_gib >= 200 )); then
        printf '%d\n' "$MAX_ROOT_GIB"
        return 0
    fi

    # Linear interpolation:
    #
    # root = 20 + (disk - 80) * 40 / 120
    #
    # Add 60 before division for nearest-integer rounding.
    root=$((20 + ((disk_gib - 80) * 40 + 60) / 120))

    if (( root < MIN_ROOT_GIB )); then
        root="$MIN_ROOT_GIB"
    fi

    if (( root > MAX_ROOT_GIB )); then
        root="$MAX_ROOT_GIB"
    fi

    printf '%d\n' "$root"
}

calculate_swap_size() {
    local ram="$1"

    if [[ ! "$ram" =~ ^[0-9]+$ ]]; then
        return 1
    fi

    if (( ram < MIN_SWAP_GIB )); then
        ram="$MIN_SWAP_GIB"
    fi

    printf '%d\n' "$ram"
}

calculate_partition_plan() {
    local disk_gib="$1"
    local reserved_gib=0

    if [[ ! "$disk_gib" =~ ^[0-9]+$ ]]; then
        error "Invalid disk size: $disk_gib"
        return 1
    fi

    if (( disk_gib < 80 )); then
        error "Disk is too small: ${disk_gib} GiB."
        error "V1 requires at least 80 GiB."
        return 1
    fi

    if [[ "$FIRMWARE_MODE" != "uefi" &&
          "$FIRMWARE_MODE" != "bios" ]]; then
        error "Unknown firmware mode: $FIRMWARE_MODE"
        return 1
    fi

    ROOT_SIZE_GIB="$(calculate_root_size "$disk_gib")" || {
        error "Unable to calculate root size."
        return 1
    }

    SWAP_SIZE_GIB="$(calculate_swap_size "$RAM_GIB")" || {
        error "Unable to calculate swap size."
        return 1
    }

    if [[ "$FIRMWARE_MODE" == "uefi" ]]; then
        reserved_gib="$EFI_SIZE_GIB"
    else
        # The BIOS Boot partition is only 2 MiB.
        # It is not counted as a whole GiB here.
        reserved_gib=0
    fi

    HOME_SIZE_GIB=$((disk_gib - reserved_gib - ROOT_SIZE_GIB - SWAP_SIZE_GIB))

    if (( HOME_SIZE_GIB < MIN_HOME_GIB )); then
        error "Calculated /home is only ${HOME_SIZE_GIB} GiB."
        error "V1 requires at least ${MIN_HOME_GIB} GiB of /home."
        return 1
    fi

    export ROOT_SIZE_GIB
    export SWAP_SIZE_GIB
    export HOME_SIZE_GIB

    return 0
}

# Create the V1 GPT partition layout using sgdisk.
#
# IMPORTANT:
#   This function is destructive.
#   It must only be called after:
#     - the disk has been explicitly selected
#     - live-media disk exclusion has succeeded
#     - the user has confirmed the exact erase operation
#     - calculate_partition_plan() has succeeded
#
# UEFI:
#   1: 1 GiB EFI System Partition
#   2: root
#   3: swap
#   4: home (remainder)
#
# Legacy BIOS:
#   1: 2 MiB BIOS Boot Partition
#   2: root
#   3: swap
#   4: home (remainder)
#
# Filesystems are NOT created here.

###################################
create_partitions_sgdisk() {
    local disk="$1"

    if [[ -z "$disk" ]]; then
        error "No installation disk was specified."
        return 1
    fi

    if [[ ! -b "$disk" ]]; then
        error "Installation target is not a block device: $disk"
        return 1
    fi

    # Final safety check:
    # Never permit the live installer device to be partitioned.
    if [[ -n "${LIVE_MEDIA_DEVICE:-}" &&
          "$disk" == "$LIVE_MEDIA_DEVICE" ]]; then
        error "REFUSING to partition the live installer device: $disk"
        return 1
    fi

    # Final eligibility check:
    # This re-applies the same disk-selection safety rule immediately
    # before any destructive operation.
    if ! disk_is_eligible "$disk"; then
        error "Installation target is no longer eligible: $disk"
        return 1
    fi

    # NEW: Comprehensive safety check for mounted filesystems, swap, LVM, RAID, DM
    if ! disk_safety_check "$disk"; then
        return 1
    fi

    # NEW: Verify disk identity hasn't changed since selection (TOCTOU protection)
    if ! disk_identity_check "$disk"; then
        return 1
    fi

    local disk_type
    disk_type="$(lsblk -dnro TYPE "$disk" 2>/dev/null)" || {
        error "Unable to determine device type: $disk"
        return 1
    }

    if [[ "$disk_type" != "disk" ]]; then
        if [[ "$disk_type" != "loop" || "${BCALCOS_DISPOSABLE_TEST:-0}" != "1" ]]; then
            error "Installation target is not a whole disk: $disk"
            return 1
        fi
    fi

    # Refuse disks with mounted filesystems anywhere in their hierarchy.
    if lsblk -nr -o MOUNTPOINTS "$disk" 2>/dev/null |
        grep -q '[^[:space:]]'; then
        error "Refusing to partition mounted disk: $disk"
        return 1
    fi

    if ! command -v sgdisk >/dev/null 2>&1; then
        error "sgdisk is required for partition creation."
        return 1
    fi

    if [[ -z "$ROOT_SIZE_GIB" ||
          -z "$SWAP_SIZE_GIB" ||
          -z "$HOME_SIZE_GIB" ]]; then
        error "Partition plan has not been calculated."
        return 1
    fi

    info "Creating GPT partition table on $disk..."

    if ! sgdisk --zap-all "$disk"; then
        error "Failed to remove existing partition tables from $disk."
        return 1
    fi

    if ! sgdisk --clear "$disk"; then
        error "Failed to create a new GPT on $disk."
        return 1
    fi

    if [[ "$FIRMWARE_MODE" == "uefi" ]]; then
        if ! sgdisk \
            --new=1:0:+1GiB \
            --typecode=1:ef00 \
            --change-name=1:EFI \
            --new=2:0:+"${ROOT_SIZE_GIB}"GiB \
            --typecode=2:8300 \
            --change-name=2:rootfs \
            --new=3:0:+"${SWAP_SIZE_GIB}"GiB \
            --typecode=3:8200 \
            --change-name=3:swap \
            --new=4:0:0 \
            --typecode=4:8300 \
            --change-name=4:home \
            "$disk"; then
            error "Failed to create UEFI partition layout."
            return 1
        fi

    elif [[ "$FIRMWARE_MODE" == "bios" ]]; then
        if ! sgdisk \
            --new=1:0:+2MiB \
            --typecode=1:ef02 \
            --change-name=1:BIOS-BOOT \
            --new=2:0:+"${ROOT_SIZE_GIB}"GiB \
            --typecode=2:8300 \
            --change-name=2:rootfs \
            --new=3:0:+"${SWAP_SIZE_GIB}"GiB \
            --typecode=3:8200 \
            --change-name=3:swap \
            --new=4:0:0 \
            --typecode=4:8300 \
            --change-name=4:home \
            "$disk"; then
            error "Failed to create BIOS partition layout."
            return 1
        fi
    else
        error "Unknown firmware mode: $FIRMWARE_MODE"
        return 1
    fi

    # Ask the kernel to reread the new partition table.
    if ! partprobe "$disk"; then
        error "Kernel failed to reread the new partition table on $disk."
        return 1
    fi

    # Give udev a chance to create the partition device nodes.
    if command_exists udevadm; then
        if ! udevadm settle; then
            error "udev failed to settle after partition-table update."
            return 1
        fi
    fi

    if ! wait_for_partitions "$disk"; then
        return 1
    fi

    info "GPT partition table created successfully on $disk."

    return 0
}

###################################
# Wait for partition device nodes to exist and verify they are accessible.
# Critical for NVMe and some USB devices where nodes appear asynchronously.
wait_for_partitions() {
    local disk="$1"
    local max_attempts=30
    local attempt=0
    local partitions=(
        "$(get_partition_path "$disk" 1)"
        "$(get_partition_path "$disk" 2)"
        "$(get_partition_path "$disk" 3)"
        "$(get_partition_path "$disk" 4)"
    )

    info "Waiting for partition device nodes to appear..."

    while ((attempt < max_attempts)); do
        ((attempt += 1))

        local all_exist=1
        local p

        for p in "${partitions[@]}"; do
            if [[ ! -b "$p" ]]; then
                all_exist=0
                break
            fi
        done

        if ((all_exist == 1)); then
            info "All partition device nodes verified."
            return 0
        fi

        sleep 0.1
    done

    error "Timeout waiting for partition device nodes."
    error "Partitions not found after ${max_attempts} attempts."

    return 1
}

###################################
# Create filesystems for the already-created V1 partition layout.
#
# IMPORTANT:
#   This function is destructive.
#   It must only be called after:
#     - partition creation has succeeded
#     - the disk has passed live-media exclusion
#     - the user has confirmed the erase operation
#
# Arguments:
#   $1 = EFI partition (UEFI only)
#   $2 = root partition
#   $3 = swap partition
#   $4 = home partition
#
# No mounting or extraction is performed here.

create_filesystems() {
    local esp="$1"
    local root="$2"
    local swap="$3"
    local home="$4"

    if [[ "$FIRMWARE_MODE" == "uefi" ]]; then
        if [[ -z "$esp" || ! -b "$esp" ]]; then
            error "Invalid EFI partition: $esp"
            return 1
        fi
    elif [[ "$FIRMWARE_MODE" != "bios" ]]; then
        error "Unknown firmware mode: $FIRMWARE_MODE"
        return 1
    fi

    if [[ -z "$root" || ! -b "$root" ]]; then
        error "Invalid root partition: $root"
        return 1
    fi

    if [[ -z "$swap" || ! -b "$swap" ]]; then
        error "Invalid swap partition: $swap"
        return 1
    fi

    if [[ -z "$home" || ! -b "$home" ]]; then
        error "Invalid home partition: $home"
        return 1
    fi

    if [[ "$FIRMWARE_MODE" == "uefi" ]]; then
        info "Formatting EFI System Partition..."
        if ! mkfs.fat -F 32 -n EFI "$esp"; then
            error "Failed to format EFI System Partition."
            return 1
        fi
    fi

    info "Formatting root filesystem..."
    if ! mkfs.ext4 -F -L rootfs "$root"; then
        error "Failed to format root filesystem."
        return 1
    fi

    info "Creating swap..."
    if ! mkswap -L swap "$swap"; then
        error "Failed to create swap."
        return 1
    fi

    info "Formatting home filesystem..."
    if ! mkfs.ext4 -F -L home "$home"; then
        error "Failed to format home filesystem."
        return 1
    fi

    success "Filesystems created successfully."

    return 0
}

####################
