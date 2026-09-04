#!/bin/bash
#COMMON.SH

INSTALLER_NAME="BCALCOS Linux Installer"
INSTALLER_VERSION="0.1.0"

SNAPSHOT=""
FIRMWARE_MODE=""
RAM_GIB=0

SELECTED_DISK=""
SELECTED_DISK_SIZE_GIB=0

EFI_SIZE_GIB=1
BIOS_BOOT_SIZE_MIB=2

ROOT_SIZE_GIB=0
SWAP_SIZE_GIB=0
HOME_SIZE_GIB=0
DISK_IDENTITY_FILE=""

require_root() {
    [[ "${EUID:-$(id -u)}" -eq 0 ]]
}

command_exists() {
    command -v "$1" >/dev/null 2>&1
}

fatal() {
    printf '\n'
    printf 'FATAL: %s\n' "$*" >&2
    exit 1
}

trim() {
    local value="$*"

    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"

    printf '%s' "$value"
}
