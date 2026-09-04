#!/bin/bash
#UI.SH

if [[ -t 1 ]]; then
    C_RESET=$'\033[0m'
    C_BOLD=$'\033[1m'
    
    # Brand colors using 256-color palette
    C_PRIMARY=$'\033[38;5;172m'        # Warm brown/orange (#BA7800)
    C_PRIMARY_DARK=$'\033[38;5;94m'    # Darker brown (#8A5A00)
    C_PRIMARY_LIGHT=$'\033[38;5;214m'  # Light orange (#D99A28)
    C_TEXT=$'\033[38;5;237m'           # Dark blue-gray (#2C3E50)
    C_SUCCESS=$'\033[38;5;71m'         # Sea green (#2E8B57)
    C_WARN=$'\033[38;5;220m'           # Golden yellow
    C_ERROR=$'\033[38;5;167m'          # Red-orange for errors
    C_INFO=$'\033[38;5;110m'           # Muted blue-gray
else
    C_RESET=""
    C_BOLD=""
    C_PRIMARY=""
    C_PRIMARY_DARK=""
    C_PRIMARY_LIGHT=""
    C_TEXT=""
    C_SUCCESS=""
    C_WARN=""
    C_ERROR=""
    C_INFO=""
fi

ui_init() {
    :
}

show_banner() {
    clear 2>/dev/null || true

    printf '\n'
    printf '%s╔══════════════════════════════════════════════════════╗%s\n' \
        "$C_PRIMARY_LIGHT$C_BOLD" "$C_RESET"
    printf '%s║              BcalcOS Linux Installer                 ║%s\n' \
        "$C_PRIMARY_LIGHT$C_BOLD" "$C_RESET"
    printf '%s║                                                      ║%s\n' \
        "$C_PRIMARY_LIGHT$C_BOLD" "$C_RESET"
    printf '%s║  Streamlined Installer for BcalcOS (bobwhite)        ║%s\n' \
        "$C_PRIMARY_LIGHT$C_BOLD" "$C_RESET"
    printf '%s╚══════════════════════════════════════════════════════╝%s\n' \
        "$C_PRIMARY_LIGHT$C_BOLD" "$C_RESET"
    printf '\n'
}

section() {
    printf '\n%s== %s ==%s\n' \
        "$C_PRIMARY$C_BOLD" "$1" "$C_RESET"
}

info() {
    printf '%s[INFO]%s %s\n' \
        "$C_INFO" "$C_RESET" "$*"
}

success() {
    printf '%s[ OK ]%s %s\n' \
        "$C_SUCCESS" "$C_RESET" "$*"
}

warning() {
    printf '%s[WARN]%s %s\n' \
        "$C_WARN" "$C_RESET" "$*"
}

error() {
    printf '%s[ERROR]%s %s\n' \
        "$C_ERROR" "$C_RESET" "$*" >&2
}

show_firmware() {
    section "Firmware"

    case "$FIRMWARE_MODE" in
        uefi)
            printf '  Firmware mode: %sUEFI%s\n' \
                "$C_SUCCESS" "$C_RESET"
            ;;
        bios)
            printf '  Firmware mode: %sLegacy BIOS%s\n' \
                "$C_SUCCESS" "$C_RESET"
            ;;
        *)
            printf '  Firmware mode: %sUNKNOWN%s\n' \
                "$C_ERROR" "$C_RESET"
            ;;
    esac
}

show_snapshot() {
    section "Live Snapshot"

    printf '  Snapshot: %s\n' "$SNAPSHOT"

    if [[ -f "$SNAPSHOT" ]]; then
        local size
        size="$(du -h -- "$SNAPSHOT" | awk '{print $1}')"
        printf '  Size:     %s\n' "$size"
    fi
}

show_memory() {
    section "Memory"

    printf '  Detected RAM: %s GiB\n' "$RAM_GIB"
    printf '  V1 swap rule: RAM × 1, minimum 2 GiB\n'
}

show_partition_plan() {
    section "Installation Plan"

    printf '\n'
    printf '  Target disk:       %s\n' "$SELECTED_DISK"
    printf '  Disk size:         %s GiB\n' "$SELECTED_DISK_SIZE_GIB"
    printf '\n'

    printf '  %-20s %-12s %-10s %s\n' \
        "Partition" "Size" "Filesystem" "Mount"

    printf '  %-20s %-12s %-10s %s\n' \
        "--------------------" \
        "------------" \
        "----------" \
        "----------"

    if [[ "$FIRMWARE_MODE" == "uefi" ]]; then
        printf '  %-20s %-12s %-10s %s\n' \
            "EFI System" \
            "1 GiB" \
            "FAT32" \
            "/boot/efi"
    else
        printf '  %-20s %-12s %-10s %s\n' \
            "BIOS Boot" \
            "2 MiB" \
            "BIOS boot" \
            "-"
    fi

    printf '  %-20s %-12s %-10s %s\n' \
        "Root" \
        "${ROOT_SIZE_GIB} GiB" \
        "ext4" \
        "/"

    printf '  %-20s %-12s %-10s %s\n' \
        "Swap" \
        "${SWAP_SIZE_GIB} GiB" \
        "swap" \
        "-"

    printf '  %-20s %-12s %-10s %s\n' \
        "Home" \
        "${HOME_SIZE_GIB} GiB" \
        "ext4" \
        "/home"

    printf '\n'
    printf '  Filesystem labels:\n'
    printf '    EFI:   EFI\n'
    printf '    Root:  rootfs\n'
    printf '    Home:  home\n'
    printf '    Swap:  swap\n'
    printf '\n'
}
####################################
show_final_confirmation() {
    printf '\n'
    printf '%s╔══════════════════════════════════════════════════════╗%s\n' \
        "$C_SUCCESS$C_BOLD" "$C_RESET"
    printf '%s║              Installation Complete                   ║%s\n' \
        "$C_SUCCESS$C_BOLD" "$C_RESET"
    printf '%s║                                                      ║%s\n' \
        "$C_SUCCESS$C_BOLD" "$C_RESET"
    printf '%s║  BcalcOS has been installed successfully.            ║%s\n' \
        "$C_SUCCESS$C_BOLD" "$C_RESET"
    printf '%s╚══════════════════════════════════════════════════════╝%s\n' \
        "$C_SUCCESS$C_BOLD" "$C_RESET"
    printf '\n'

    printf '  Installation Summary:\n'
    printf '  ----------------------\n'
    printf '  Disk:        %s\n' "$SELECTED_DISK"
    printf '  Hostname:    %s\n' "$INSTALL_HOSTNAME"
    printf '  Username:    %s\n' "$INSTALL_USERNAME"
    printf '  Timezone:    %s\n' "$INSTALL_TIMEZONE"
    printf '  Firmware:    %s\n' \
        "$([[ "$FIRMWARE_MODE" == "uefi" ]] && echo "UEFI" || echo "Legacy BIOS")"
    printf '\n'
}




