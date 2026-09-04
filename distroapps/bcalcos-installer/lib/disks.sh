#!/bin/bash
#DISKS.SH

MIN_DISK_SIZE_GIB=80

GIB_BYTES=1073741824
MIN_DISK_BYTES=$((MIN_DISK_SIZE_GIB * GIB_BYTES))

LIVE_MEDIA_DEVICE=""
DISK_IDENTITY_FILE=""

disk_size_bytes() {
    local disk="$1"
    local bytes

    bytes="$(lsblk -dnbo SIZE -- "$disk" 2>/dev/null)" || return 1

    if [[ ! "$bytes" =~ ^[0-9]+$ ]]; then
        return 1
    fi

    printf '%s\n' "$bytes"
}

disk_size_gib() {
    local disk="$1"
    local bytes

    bytes="$(disk_size_bytes "$disk")" || return 1

    printf '%d\n' "$((bytes / GIB_BYTES))"
}

disk_model() {
    local disk="$1"
    local model

    model="$(lsblk -dnro MODEL -- "$disk" 2>/dev/null)" || return 1

    model="$(trim "$model")"

    # lsblk may return escaped spaces in model strings.
    model="${model//\\x20/ }"

    if [[ -z "$model" ]]; then
        model="Unknown"
    fi

    printf '%s\n' "$model"
}

disk_is_eligible() {
    local disk="$1"
    local bytes

    # Never allow the device containing the live installer media.
    if [[ -n "$LIVE_MEDIA_DEVICE" &&
          "$disk" == "$LIVE_MEDIA_DEVICE" ]]; then
        return 1
    fi

    bytes="$(disk_size_bytes "$disk")" || return 1

    ((bytes >= MIN_DISK_BYTES))
}
######################
detect_live_media_device() {
    local source
    local parent
    local target

    LIVE_MEDIA_DEVICE=""

    # Check common live-medium mount locations.
    local targets=(
        "/run/live/medium"
        "/lib/live/mount/medium"
        "/cdrom"
    )

    # Also support a live ISO that is mounted by the desktop/session.
    # This is useful during development/testing when the ISO is plugged
    # into an already-running system.
    while IFS= read -r target; do
        [[ -n "$target" ]] || continue
        targets+=("$target")
    done < <(
        findmnt -rn -o TARGET,FSTYPE 2>/dev/null |
            awk '$2 == "iso9660" {print $1}'
    )

    for target in "${targets[@]}"; do
        [[ -d "$target" ]] || continue

        source="$(findmnt -nro SOURCE --target "$target" 2>/dev/null)" || continue
        [[ -n "$source" ]] || continue

        # Resolve a partition such as /dev/sdd1 to its whole disk /dev/sdd.
        parent="$(lsblk -no PKNAME -- "$source" 2>/dev/null | head -n1)" || true

        if [[ -n "$parent" ]]; then
            LIVE_MEDIA_DEVICE="/dev/$parent"
            export LIVE_MEDIA_DEVICE
            return 0
        fi

        # If SOURCE itself is a disk device, use it directly.
        if [[ "$source" == /dev/* ]] &&
           [[ "$(lsblk -dnro TYPE -- "$source" 2>/dev/null)" == "disk" ]]; then
            LIVE_MEDIA_DEVICE="$source"
            export LIVE_MEDIA_DEVICE
            return 0
        fi
    done

    return 1
}

#####################################3
get_candidate_disks() {
    local disk
    local type
    local removable

    while IFS= read -r disk; do
        [[ -n "$disk" ]] || continue

        type="$(lsblk -dnro TYPE -- "$disk" 2>/dev/null)" || continue
        removable="$(lsblk -dnro RM -- "$disk" 2>/dev/null)" || continue

        [[ "$type" == "disk" ]] || continue
        [[ "$removable" == "0" ]] || continue

        if disk_is_eligible "$disk"; then
            printf '%s\n' "$disk"
        fi
    done < <(
        lsblk -dnpro NAME
    )
}

list_candidate_disks() {
    local disks=()
    local disk
    local index=0
    local size
    local model

    while IFS= read -r disk; do
        [[ -n "$disk" ]] || continue
        disks+=("$disk")
    done < <(get_candidate_disks)

    printf '\n'
    printf '  %-4s %-18s %-12s %s\n' \
        "#" "Device" "Size" "Model"

    printf '  %-4s %-18s %-12s %s\n' \
        "---" \
        "------------------" \
        "------------" \
        "------------------------------"

    for disk in "${disks[@]}"; do
        ((index += 1))

        size="$(disk_size_gib "$disk")" || size="?"
        model="$(disk_model "$disk")" || model="Unknown"

        printf '  %-4s %-18s %-12s %s\n' \
            "$index" \
            "$disk" \
            "${size} GiB" \
            "$model"
    done

    printf '\n'

    if [[ -n "$LIVE_MEDIA_DEVICE" ]]; then
        printf '  Live installer device: %s (excluded)\n' \
            "$LIVE_MEDIA_DEVICE"
    else
        printf '  Live installer device: none detected\n'
    fi

    printf '  Only non-removable disks of at least %s GiB are shown.\n' \
        "$MIN_DISK_SIZE_GIB"
}
###########################
select_installation_disk() {
    local disks=()
    local disk
    local choice
    local count
    local model

    while IFS= read -r disk; do
        [[ -n "$disk" ]] || continue
        disks+=("$disk")
    done < <(get_candidate_disks)

    count="${#disks[@]}"

    if ((count == 0)); then
        error "No eligible installation disks were found."
        error "The minimum supported disk size is ${MIN_DISK_SIZE_GIB} GiB."

        return 1
    fi

    section "Installation Disk"

    list_candidate_disks

    while true; do
        printf '  Select installation disk [1-%d]: ' "$count"

        if ! read -r choice; then
            printf '\n'
            return 1
        fi

        if [[ "$choice" =~ ^[0-9]+$ ]] &&
           ((choice >= 1 && choice <= count)); then

            SELECTED_DISK="${disks[$((choice - 1))]}"

            SELECTED_DISK_SIZE_GIB="$(
                disk_size_gib "$SELECTED_DISK"
            )" || {
                error "Unable to determine selected disk size."
                return 1
            }

            model="$(disk_model "$SELECTED_DISK")" || model="Unknown"

            printf '\n'
            info "Selected disk: $SELECTED_DISK"
            info "Disk size:     ${SELECTED_DISK_SIZE_GIB} GiB"
            info "Disk model:    $model"

            export SELECTED_DISK
            export SELECTED_DISK_SIZE_GIB

            # Save disk identity for TOCTOU verification before partitioning
            DISK_IDENTITY_FILE="$(mktemp /tmp/bcalcos-disk-identity.XXXXXX)" || {
                error "Unable to create disk identity record."
                return 1
            }
            chmod 600 "$DISK_IDENTITY_FILE"

            local serial wwn
            serial="$(lsblk -dnro SERIAL "$SELECTED_DISK" 2>/dev/null)" || serial=""
            wwn="$(lsblk -dnro WWN "$SELECTED_DISK" 2>/dev/null)" || wwn=""

            printf 'size=%s\nmodel=%s\nserial=%s\nwwn=%s\n' \
                "$(disk_size_bytes "$SELECTED_DISK")" \
                "$model" \
                "$serial" \
                "$wwn" > "$DISK_IDENTITY_FILE"

            export DISK_IDENTITY_FILE

            return 0
        fi

        warning "Invalid selection."
    done
}
#####################################
# Comprehensive pre-partitioning safety check.
# Verifies disk is safe to erase:
#   - no mounted filesystems on any partition
#   - no active swap on any partition
#   - no LVM physical volumes
#   - no md RAID members
#   - no device-mapper mappings
#
# Returns 0 if disk is safe, non-zero otherwise.
disk_safety_check() {
    local disk="$1"
    local reason=""
    local disk_base="${disk##*/}"
    local partitions
    local p

    # Get all partitions on this disk (NOT using -d, which suppresses children)
    partitions="$(lsblk -nro NAME "$disk" 2>/dev/null | grep -v "^${disk_base}$")" || true

    # Check for mounted partitions
    local mp
    while IFS= read -r mp; do
        if [[ -n "$mp" && "$mp" != "[none]" && "$mp" != "none" ]]; then
            reason="mounted filesystem: $mp"
            break
        fi
    done < <(lsblk -nr -o MOUNTPOINTS "$disk" 2>/dev/null)

    if [[ -n "$reason" ]]; then
        error "Refusing to partition disk with mounted filesystems: $disk ($reason)"
        return 1
    fi

    # Check for active swap on any partition of this disk
    if command -v swapon >/dev/null 2>&1; then
        local swap_dev
        while IFS= read -r swap_dev; do
            [[ -n "$swap_dev" ]] || continue
            if [[ "$swap_dev" == "$disk"* ]]; then
                reason="active swap on $swap_dev"
                break
            fi
        done < <(swapon --show=NAME 2>/dev/null)

        if [[ -n "$reason" ]]; then
            error "Refusing to partition disk with active swap: $disk ($reason)"
            return 1
        fi
    fi

    # Check for LVM physical volumes
    if command -v pvs >/dev/null 2>&1; then
        local pv_dev
        while IFS= read -r pv_dev; do
            [[ -n "$pv_dev" ]] || continue
            if [[ "$pv_dev" == "$disk"* ]]; then
                reason="LVM physical volume on $pv_dev"
                break
            fi
        done < <(pvs --noheadings -o pv_name 2>/dev/null | tr -d ' ')

        if [[ -n "$reason" ]]; then
            error "Refusing to partition disk with LVM: $disk ($reason)"
            return 1
        fi
    fi

    # Check for md RAID members
    if command -v mdadm >/dev/null 2>&1; then
        local raid_dev
        for raid_dev in $partitions; do
            [[ -n "$raid_dev" ]] || continue
            raid_dev="/dev/$raid_dev"
            if mdadm --examine "$raid_dev" &>/dev/null; then
                reason="RAID member on $raid_dev"
                break
            fi
        done

        if [[ -n "$reason" ]]; then
            error "Refusing to partition disk with RAID: $disk ($reason)"
            return 1
        fi
    fi

    # Check for device-mapper mappings
    if [[ -d /dev/mapper ]]; then
        local dm_dev
        for dm_dev in /dev/mapper/*; do
            [[ -e "$dm_dev" ]] || continue
            if lsblk -no PKNAME "$dm_dev" 2>/dev/null | grep -q "^${disk_base}$"; then
                reason="device-mapper mapping from $dm_dev"
                break
            fi
        done

        if [[ -n "$reason" ]]; then
            error "Refusing to partition disk with device-mapper: $disk ($reason)"
            return 1
        fi
    fi

    return 0
}

#####################################
# Verify disk identity hasn't changed since selection (TOCTOU protection).
# Compares current disk properties against values saved at selection time.
#####################################
# Verify disk identity hasn't changed since selection (TOCTOU protection).
# Compares current disk properties against values saved at selection time.
disk_identity_check() {
    local disk="$1"

    if [[ -z "$DISK_IDENTITY_FILE" || ! -f "$DISK_IDENTITY_FILE" ]]; then
        error "No disk identity found for $disk."
        error "Disk was not properly selected before installation."
        return 1
    fi

    local stored_size stored_model stored_serial stored_wwn
    stored_size="$(grep '^size=' "$DISK_IDENTITY_FILE" | cut -d= -f2)"
    stored_model="$(grep '^model=' "$DISK_IDENTITY_FILE" | cut -d= -f2)"
    stored_serial="$(grep '^serial=' "$DISK_IDENTITY_FILE" | cut -d= -f2)"
    stored_wwn="$(grep '^wwn=' "$DISK_IDENTITY_FILE" | cut -d= -f2)"

    local current_size current_model current_serial current_wwn
    current_size="$(disk_size_bytes "$disk")" || return 1
    current_model="$(disk_model "$disk")" || return 1
    current_serial="$(lsblk -dnro SERIAL "$disk" 2>/dev/null)" || current_serial=""
    current_wwn="$(lsblk -dnro WWN "$disk" 2>/dev/null)" || current_wwn=""

    if [[ "$current_size" != "$stored_size" ]]; then
        error "Disk size changed since selection!"
        error "  Original: $stored_size bytes"
        error "  Current:  $current_size bytes"
        return 1
    fi

    if [[ "$current_model" != "$stored_model" ]]; then
        error "Disk model changed since selection!"
        error "  Original: $stored_model"
        error "  Current:  $current_model"
        return 1
    fi

    # Only compare serial if one was recorded at selection time
    if [[ -n "$stored_serial" && "$current_serial" != "$stored_serial" ]]; then
        error "Disk serial changed since selection!"
        error "  Original: $stored_serial"
        error "  Current:  $current_serial"
        return 1
    fi

    # Only compare WWN if one was recorded at selection time
    if [[ -n "$stored_wwn" && "$current_wwn" != "$stored_wwn" ]]; then
        error "Disk WWN changed since selection!"
        error "  Original: $stored_wwn"
        error "  Current:  $current_wwn"
        return 1
    fi

    info "Disk identity verified: $disk"
    return 0
}

#####################################
# Verify BIOS Boot partition has correct GPT type.
# Returns 0 if valid, non-zero otherwise.
verify_bios_boot_partition() {
    local disk="$1"
    local bios_part

    # Determine the BIOS Boot partition path (always partition 1 for GPT/BIOS)
    bios_part="$(get_partition_path "$disk" 1)"

    if [[ ! -b "$bios_part" ]]; then
        error "BIOS Boot partition does not exist: $bios_part"
        return 1
    fi

    # Get the partition type via sgdisk -i
    # sgdisk -i reports the GUID, e.g.:
    #   Partition GUID code: 21686148-6449-6E6F-744E-656564454649 (BIOS boot partition)
    local typecode
    typecode="$(sgdisk -i 1 "$disk" 2>/dev/null | grep -E 'Partition GUID code' | awk '{print $4}')" || {
        error "Unable to read BIOS Boot partition type code."
        return 1
    }

    # BIOS boot partition GUID: 21686148-6449-6E6F-744E-656564454649
    if [[ "$typecode" != "21686148-6449-6E6F-744E-656564454649" ]]; then
        error "BIOS Boot partition has wrong type code: $typecode"
        error "Expected: 21686148-6449-6E6F-744E-656564454649 (BIOS boot partition)"
        return 1
    fi

    info "BIOS Boot partition verified: $bios_part"
    return 0
}




