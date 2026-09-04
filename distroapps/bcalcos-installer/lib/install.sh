#!/bin/bash
#INSTALL.SH

# BCALCOS installation target operations.
#
# This stage handles:
#   - mounting the already-formatted target
#   - extracting filesystem.squashfs
#   - unmounting the target
#   - modifying users (transform, password, admin/sudo)
#   - generating fstab
#   - generating hostname/hosts
#   - generating machine-id
#   - configuring hibernation/resume
#   - cleaning up live-boot components
#   - configuring GRUB (BIOS/UEFI install, branding, config generation)
#   - chroot operations (grub-mkconfig, update-initramfs, dpkg)
#
# It does NOT:
#   - partition disks (handled by partition.sh)
#   - format filesystems (handled by partition.sh)
#
# The caller is responsible for ensuring that the target disk has
# already passed all safety checks and that the user has confirmed
# installation.

TARGET_ROOT=""
TARGET_HOME=""
TARGET_EFI=""

mount_target() {
    local root_partition="$1"
    local home_partition="$2"
    local esp_partition="${3:-}"

    if [[ -z "$root_partition" || ! -b "$root_partition" ]]; then
        error "Invalid root partition: $root_partition"
        return 1
    fi

    if [[ -z "$home_partition" || ! -b "$home_partition" ]]; then
        error "Invalid home partition: $home_partition"
        return 1
    fi

    if [[ "$FIRMWARE_MODE" == "uefi" ]]; then
        if [[ -z "$esp_partition" || ! -b "$esp_partition" ]]; then
            error "Invalid EFI partition: $esp_partition"
            return 1
        fi
    elif [[ "$FIRMWARE_MODE" != "bios" ]]; then
        error "Unknown firmware mode: $FIRMWARE_MODE"
        return 1
    fi

    TARGET_ROOT="$(mktemp -d /tmp/bcalcos-target.XXXXXX)" || {
        error "Unable to create temporary target mount directory."
        return 1
    }

    TARGET_HOME="$TARGET_ROOT/home"

    info "Mounting root filesystem..."
    if ! mount "$root_partition" "$TARGET_ROOT"; then
        error "Failed to mount root filesystem."
        rmdir "$TARGET_ROOT" 2>/dev/null || true
        TARGET_ROOT=""
        TARGET_HOME=""
        return 1
    fi

    mkdir -p "$TARGET_HOME"

    info "Mounting home filesystem..."
    if ! mount "$home_partition" "$TARGET_HOME"; then
        error "Failed to mount home filesystem."
        umount "$TARGET_ROOT" 2>/dev/null || true
        rmdir "$TARGET_ROOT" 2>/dev/null || true
        TARGET_ROOT=""
        TARGET_HOME=""
        return 1
    fi

    if [[ "$FIRMWARE_MODE" == "uefi" ]]; then
        TARGET_EFI="$TARGET_ROOT/boot/efi"

        mkdir -p "$TARGET_EFI"

        info "Mounting EFI System Partition..."
        if ! mount "$esp_partition" "$TARGET_EFI"; then
            error "Failed to mount EFI System Partition."
            umount "$TARGET_HOME" 2>/dev/null || true
            umount "$TARGET_ROOT" 2>/dev/null || true
            rmdir "$TARGET_ROOT" 2>/dev/null || true
            TARGET_ROOT=""
            TARGET_HOME=""
            TARGET_EFI=""
            return 1
        fi
    fi

    success "Target filesystems mounted."

    return 0
}


extract_snapshot() {
    local snapshot="$1"

    if [[ -z "$TARGET_ROOT" || ! -d "$TARGET_ROOT" ]]; then
        error "Target root is not mounted."
        return 1
    fi

    if [[ -z "$snapshot" || ! -f "$snapshot" || ! -r "$snapshot" ]]; then
        error "Invalid filesystem snapshot: $snapshot"
        return 1
    fi

    info "Extracting filesystem snapshot..."
    info "Source: $snapshot"
    info "Target: $TARGET_ROOT"

    if ! unsquashfs -f -d "$TARGET_ROOT" "$snapshot"; then
        error "Snapshot extraction failed."
        return 1
    fi

    success "Filesystem snapshot extracted successfully."

    return 0
}


unmount_target() {
    local failed=0

    if [[ -n "$TARGET_EFI" && -d "$TARGET_EFI" ]]; then
        info "Unmounting EFI System Partition..."
        if ! umount "$TARGET_EFI"; then
            error "Failed to unmount EFI System Partition."
            failed=1
        fi
    fi

    if [[ -n "$TARGET_HOME" && -d "$TARGET_HOME" ]]; then
        info "Unmounting home filesystem..."
        if ! umount "$TARGET_HOME"; then
            error "Failed to unmount home filesystem."
            failed=1
        fi
    fi

    if [[ -n "$TARGET_ROOT" && -d "$TARGET_ROOT" ]]; then
        info "Unmounting root filesystem..."
        if ! umount "$TARGET_ROOT"; then
            error "Failed to unmount root filesystem."
            failed=1
        fi
    fi

    if (( failed != 0 )); then
        return 1
    fi

    rmdir "$TARGET_ROOT" 2>/dev/null || true

    TARGET_ROOT=""
    TARGET_HOME=""
    TARGET_EFI=""

    success "Target filesystems unmounted."

    return 0
}
#########################3
transform_installed_user() {
    local new_username="$1"
    local old_username="isouser"

    if [[ -z "$TARGET_ROOT" || ! -d "$TARGET_ROOT" ]]; then
        error "Target root is not mounted."
        return 1
    fi

    if [[ -z "$new_username" ]]; then
        error "Username cannot be empty."
        return 1
    fi

    # V1 username policy:
    # lowercase letters, digits, underscore, hyphen
    # must begin with a lowercase letter or underscore
    if [[ ! "$new_username" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]]; then
        error "Invalid username: $new_username"
        error "Use 1-32 characters: lowercase letters, digits, '_' or '-'."
        return 1
    fi

    if [[ "$new_username" == "$old_username" ]]; then
        error "New username must differ from $old_username."
        return 1
    fi

    if ! grep -qE "^${old_username}:" "$TARGET_ROOT/etc/passwd"; then
        error "Template user '$old_username' was not found in target."
        return 1
    fi

    if grep -qE "^${new_username}:" "$TARGET_ROOT/etc/passwd"; then
        error "Username '$new_username' already exists in target."
        return 1
    fi

    info "Transforming $old_username into $new_username..."

    if ! usermod \
        --root "$TARGET_ROOT" \
        --login "$new_username" \
        --home "/home/$new_username" \
        --move-home \
	--comment "$new_username" \
        "$old_username"; then
        error "Failed to rename installed user."
        return 1
    fi

    # Preserve the existing primary GID while changing the group name
    # from isouser to the requested username when that group exists.
    if grep -qE "^${old_username}:" "$TARGET_ROOT/etc/group"; then
        if ! groupmod \
            --root "$TARGET_ROOT" \
            --new-name "$new_username" \
            "$old_username"; then
            error "Failed to rename primary group."
            return 1
        fi
    fi

    # Ensure the transformed home directory exists.
    if [[ ! -d "$TARGET_ROOT/home/$new_username" ]]; then
        error "Transformed home directory was not created."
        return 1
    fi

    success "User transformed: $old_username -> $new_username"
    success "Home preserved: /home/$new_username"

    return 0
}
####################################
configure_admin_user() {
    local username="$1"

    if [[ -z "$TARGET_ROOT" || ! -d "$TARGET_ROOT" ]]; then
        error "Target root is not mounted."
        return 1
    fi

    if [[ -z "$username" ]]; then
        error "Username cannot be empty."
        return 1
    fi

    if ! grep -qE "^${username}:" "$TARGET_ROOT/etc/passwd"; then
        error "User '$username' was not found in target."
        return 1
    fi

    # Lock the root account (disable password-based login)
    info "Locking root account..."

    if ! usermod --root "$TARGET_ROOT" --lock root; then
        error "Failed to lock root account."
        return 1
    fi

    success "Root account locked."

    # Add the new user to the sudo group
    info "Adding $username to sudo group..."

    if ! usermod --root "$TARGET_ROOT" --append --groups sudo "$username"; then
        error "Failed to add $username to sudo group."
        return 1
    fi

    success "$username granted sudo access."

    return 0
}
#################################
set_installed_user_password() {
    local username="$1"
    local password="$2"
    local password_confirm="$3"
    local password_hash

    if [[ -z "$TARGET_ROOT" || ! -d "$TARGET_ROOT" ]]; then
        error "Target root is not mounted."
        return 1
    fi

    if [[ -z "$username" ]]; then
        error "Username cannot be empty."
        return 1
    fi

    if [[ "$password" != "$password_confirm" ]]; then
        error "Passwords do not match."
        return 1
    fi

    if [[ -z "$password" ]]; then
        error "Password cannot be empty."
        return 1
    fi

    if ! grep -qE "^${username}:" "$TARGET_ROOT/etc/passwd"; then
        error "User '$username' was not found in target."
        return 1
    fi

    if ! command_exists mkpasswd; then
        error "mkpasswd is required for password creation."
        return 1
    fi

    if ! command_exists usermod; then
        error "usermod is required for password installation."
        return 1
    fi

    info "Generating new password hash..."

    password_hash="$(
        printf '%s\n' "$password" |
            mkpasswd -m sha-512 -s
    )" || {
        error "Unable to generate password hash."
        return 1
    }

    if [[ -z "$password_hash" ]]; then
        error "Password hash generation returned empty."
        return 1
    fi

    info "Installing new password for $username..."

    if ! usermod \
        --root "$TARGET_ROOT" \
        --password "$password_hash" \
        "$username"; then
        error "Failed to install password for $username."
        return 1
    fi

    success "New password installed for $username."

    return 0
}
#######################
set_install_timezone() {
    local timezone="$1"
    local zoneinfo_path

    if [[ -z "$TARGET_ROOT" || ! -d "$TARGET_ROOT" ]]; then
        error "Target root is not mounted."
        return 1
    fi

    if [[ -z "$timezone" ]]; then
        error "Timezone cannot be empty."
        return 1
    fi

    zoneinfo_path="$TARGET_ROOT/usr/share/zoneinfo/$timezone"

    if [[ ! -f "$zoneinfo_path" ]]; then
        error "Invalid timezone: $timezone"
        return 1
    fi

    info "Setting installed system timezone: $timezone"

    rm -f "$TARGET_ROOT/etc/localtime"

    if ! ln -s "/usr/share/zoneinfo/$timezone" \
        "$TARGET_ROOT/etc/localtime"; then
        error "Failed to set /etc/localtime."
        return 1
    fi

    printf '%s\n' "$timezone" > "$TARGET_ROOT/etc/timezone"

    if [[ ! -s "$TARGET_ROOT/etc/timezone" ]]; then
        error "Failed to create /etc/timezone."
        return 1
    fi

    success "Timezone configured: $timezone"

    return 0
}
#########################################
generate_hostname() {
    local hostname="$1"

    if [[ -z "$TARGET_ROOT" || ! -d "$TARGET_ROOT" ]]; then
        error "Target root is not mounted."
        return 1
    fi

    if [[ -z "$hostname" ]]; then
        error "Hostname cannot be empty."
        return 1
    fi

    info "Configuring hostname: $hostname"

    # Write /etc/hostname
    printf '%s\n' "$hostname" > "$TARGET_ROOT/etc/hostname"

    if [[ ! -s "$TARGET_ROOT/etc/hostname" ]]; then
        error "Failed to create /etc/hostname."
        return 1
    fi

    # Write /etc/hosts with the new hostname
    {
        printf '%s\n' '127.0.0.1   localhost'
        printf '127.0.1.1   %s.localdomain %s\n' "$hostname" "$hostname"
        printf '%s\n' '::1         ip6-localhost ip6-loopback'
        printf '%s\n' 'fe00::0     ip6-localnet'
        printf '%s\n' 'ff00::0     ip6-mcastprefix'
        printf '%s\n' 'ff02::1     ip6-allnodes'
        printf '%s\n' 'ff02::2     ip6-allrouters'
    } > "$TARGET_ROOT/etc/hosts"

    if [[ ! -s "$TARGET_ROOT/etc/hosts" ]]; then
        error "Failed to create /etc/hosts."
        return 1
    fi

    success "Hostname configured: $hostname"

    return 0
}

#########################################
configure_hibernation() {
    local swap_partition="$1"
    local swap_uuid
    local grub_default="$TARGET_ROOT/etc/default/grub"

    if [[ -z "$TARGET_ROOT" || ! -d "$TARGET_ROOT" ]]; then
        error "Target root is not mounted."
        return 1
    fi

    if [[ -z "$swap_partition" || ! -b "$swap_partition" ]]; then
        error "Invalid swap partition: $swap_partition"
        return 1
    fi

    info "Configuring hibernation/resume..."

    # Get swap UUID
    swap_uuid="$(blkid -s UUID -o value "$swap_partition")" || {
        error "Unable to read swap filesystem UUID."
        return 1
    }

    if [[ -z "$swap_uuid" ]]; then
        error "Swap filesystem UUID is missing."
        return 1
    fi

    # Create directory for resume config if it doesn't exist
    mkdir -p "$TARGET_ROOT/etc/initramfs-tools/conf.d"

    # Write RESUME variable to initramfs config
    printf 'RESUME=UUID=%s\n' "$swap_uuid" > "$TARGET_ROOT/etc/initramfs-tools/conf.d/resume"

    if [[ ! -s "$TARGET_ROOT/etc/initramfs-tools/conf.d/resume" ]]; then
        error "Failed to create /etc/initramfs-tools/conf.d/resume."
        return 1
    fi

    success "Initramfs resume configured: $swap_uuid"

    # Also add resume=UUID to GRUB kernel command line
    # This ensures the kernel knows which device to resume from at boot
    if [[ -f "$grub_default" ]]; then
        info "Updating GRUB kernel parameters..."

        # Create backup
        cp -f "$grub_default" "${grub_default}.installer-backup"

        # Read current GRUB_CMDLINE_LINUX_DEFAULT
        local current_params
        current_params="$(grep '^GRUB_CMDLINE_LINUX_DEFAULT=' "$grub_default" | head -1)"

        if [[ -n "$current_params" ]]; then
            # Check if resume= is already present
            if echo "$current_params" | grep -q 'resume='; then
                info "GRUB already contains resume parameter; leaving unchanged."
            else
                # Extract current value, append resume=UUID, and rewrite
                local quoted_params
                quoted_params="$(echo "$current_params" | sed 's/GRUB_CMDLINE_LINUX_DEFAULT=\(.*\)/\1/' | tr -d '"')"

                if [[ -z "$quoted_params" ]]; then
                    printf 'GRUB_CMDLINE_LINUX_DEFAULT="resume=UUID=%s"\n' "$swap_uuid" >> "$grub_default"
                else
                    sed -i "s/^GRUB_CMDLINE_LINUX_DEFAULT=.*/GRUB_CMDLINE_LINUX_DEFAULT=\"${quoted_params} resume=UUID=${swap_uuid}\"/" "$grub_default"
                fi

                success "Added resume=UUID to GRUB_CMDLINE_LINUX_DEFAULT"
            fi
        fi
    fi

    return 0
}
####################################
cleanup_live_boot() {
    if [[ -z "$TARGET_ROOT" || ! -d "$TARGET_ROOT" ]]; then
        error "Target root is not mounted."
        return 1
    fi

    info "Removing live-boot components..."

    # Step 1: Check which packages are actually installed
    local packages_to_remove=(
        live-boot
        live-config
        live-config-sysvinit
        live-tools
        refractasnapshot-base
        refractasnapshot-gui
        refractainstaller-base
        refractainstaller-gui
        refracta2usb
    )   
    local installed_packages=()
    local pkg

    for pkg in "${packages_to_remove[@]}"; do
        if chroot "$TARGET_ROOT" dpkg -s "$pkg" &>/dev/null; then
            installed_packages+=("$pkg")
        fi
    done

    # Step 2: Attempt removal if any packages were found
    if (( ${#installed_packages[@]} > 0 )); then
        if ! chroot "$TARGET_ROOT" dpkg --purge --force-depends "${installed_packages[@]}" 2>/dev/null; then
            error "Failed to remove live-boot packages:"
            printf '  %s\n' "${installed_packages[@]}"
            return 1
        fi

        info "Live-boot packages removed."
    else
        info "No live-boot packages found in snapshot."
    fi

    # Step 3: Verify packages are actually gone from dpkg database
    for pkg in "${installed_packages[@]}"; do
        if chroot "$TARGET_ROOT" dpkg -s "$pkg" &>/dev/null; then
            warning "Package $pkg could not be removed. Continuing..."
        fi
    done

    # Step 4: Remove /etc/live configuration directory
    if [[ -d "$TARGET_ROOT/etc/live" ]]; then
        rm -rf "$TARGET_ROOT/etc/live"
        info "Removed /etc/live"
    fi

    # Step 5: Remove anacron diversion created by live-config (if present)
    if [[ -f "$TARGET_ROOT/var/lib/dpkg/diversions" ]]; then
        if grep -q 'anacron' "$TARGET_ROOT/var/lib/dpkg/diversions" 2>/dev/null; then
            if chroot "$TARGET_ROOT" dpkg-divert --remove --rename /etc/cron.d/anacron 2>/dev/null; then
                info "Removed anacron diversion"
            fi
        fi
    fi

    # Step 6: Remove leftover polkit rules from live-config (if present)
    local polkit_dir="$TARGET_ROOT/etc/polkit-1/rules.d"
    if [[ -d "$polkit_dir" ]]; then
        local removed=0
        if ls "$polkit_dir"/45-*live* 2>/dev/null >/dev/null; then
            rm -f "$polkit_dir"/45-*live* 2>/dev/null && ((removed=1))
        fi
        if ls "$polkit_dir"/49-*live* 2>/dev/null >/dev/null; then
            rm -f "$polkit_dir"/49-*live* 2>/dev/null && ((removed=1))
        fi
        if (( removed == 1 )); then
            info "Removed polkit live rules"
        fi
    fi

    success "Live-boot components removed."

    return 0
}

########################################
cleanup() {
    local grub_backup="$TARGET_ROOT/etc/default/grub.installer-backup"

    if [[ -z "$TARGET_ROOT" || ! -d "$TARGET_ROOT" ]]; then
        error "Target root is not mounted."
        return 1
    fi

    info "Cleaning up installation artifacts..."

    # Remove GRUB backup file created during hibernation configuration
    if [[ -f "$grub_backup" ]]; then
        rm -f "$grub_backup"
        info "Removed GRUB backup: ${grub_backup#"$TARGET_ROOT"}"
    fi

    # Remove installer icon from user's desktop (keep Start menu entry)
    local desktop_file="$TARGET_ROOT/home/$INSTALL_USERNAME/Desktop/bcalcos-installer.desktop"
    if [[ -f "$desktop_file" ]]; then
        rm -f "$desktop_file"
        info "Removed installer desktop shortcut"
    fi

    # Remove stale keyring files from the build environment
    # These were encrypted with isouser's password and will cause
    # "Unlock Login Keyring" prompts for the new user
    local keyring_dir="$TARGET_ROOT/home/$INSTALL_USERNAME/.local/share/keyrings"
    if [[ -d "$keyring_dir" ]]; then
        rm -f "$keyring_dir"/*.keyring
        info "Removed stale login keyring"
    fi

    info "Configured bcalcos-welcome for first boot"

    success "Cleanup completed."

    return 0
}


####################################
verify_installation() {
    local username="$1"
    local passed=0
    local failed=0
    local check

    if [[ -z "$TARGET_ROOT" || ! -d "$TARGET_ROOT" ]]; then
        error "Target root is not mounted."
        return 1
    fi

    section "Verifying Installation"

    # Helper: check file exists and is non-empty
    check() {
        local description="$1"
        local path="$2"

        if [[ -s "$path" ]]; then
            success "$description"
            ((passed += 1))
        else
            error "$description (FAILED: $path)"
            ((failed += 1))
        fi
    }

    # Helper: check file does NOT exist
    check_absent() {
        local description="$1"
        local path="$2"

        if [[ ! -e "$path" ]]; then
            success "$description"
            ((passed += 1))
        else
            error "$description (FAILED: $path still exists)"
            ((failed += 1))
        fi
    }

    # Helper: check grep match
    check_grep() {
        local description="$1"
        local pattern="$2"
        local file="$3"

        if grep -qE "$pattern" "$file" 2>/dev/null; then
            success "$description"
            ((passed += 1))
        else
            error "$description (FAILED)"
            ((failed += 1))
        fi
    }

    # Core files
    check "fstab exists" \
        "$TARGET_ROOT/etc/fstab"
    check "hostname exists" \
        "$TARGET_ROOT/etc/hostname"
    check "hosts exists" \
        "$TARGET_ROOT/etc/hosts"
    check "machine-id exists" \
        "$TARGET_ROOT/etc/machine-id"
    check "grub.cfg exists" \
        "$TARGET_ROOT/boot/grub/grub.cfg"

    if [[ -n "$TARGET_EFI" ]]; then
        check_grep "UEFI fallback stub present" \
            '^search .*--fs-uuid' "$TARGET_EFI/EFI/BOOT/grub.cfg"
        check_grep "UEFI boot-path stub present" \
            '^search .*--fs-uuid' "$TARGET_EFI/boot/grub/grub.cfg"
    fi

    # User and root
    check_grep "User '$username' exists in passwd" \
        "^${username}:" "$TARGET_ROOT/etc/passwd"
    check_grep "Root account is locked" \
        '^root:!' "$TARGET_ROOT/etc/shadow"

    # Hibernation
    check "Resume config exists" \
        "$TARGET_ROOT/etc/initramfs-tools/conf.d/resume"
    check_grep "Swap UUID present in fstab" \
        '^UUID=.*none.*swap' "$TARGET_ROOT/etc/fstab"

    # Live-boot cleanup
    check_absent "/etc/live removed" \
        "$TARGET_ROOT/etc/live"

    printf '\n'
    printf '  Passed: %d\n' "$passed"
    printf '  Failed: %d\n' "$failed"

    if (( failed > 0 )); then
        error "Installation verification failed."
        return 1
    fi

    success "All verification checks passed."

    return 0
}

#################################
generate_machine_id() {
    if [[ -z "$TARGET_ROOT" || ! -d "$TARGET_ROOT" ]]; then
        error "Target root is not mounted."
        return 1
    fi

    info "Generating machine-id..."

    local machine_id

    # /proc/sys/kernel/random/uuid produces a UUID v4.
    # Stripping dashes yields the 32-character hex string
    # expected by /etc/machine-id.
    machine_id="$(cat /proc/sys/kernel/random/uuid | tr -d '-')"

    if [[ -z "$machine_id" || ${#machine_id} -ne 32 ]]; then
        error "Failed to generate a valid machine-id."
        return 1
    fi

    printf '%s\n' "$machine_id" > "$TARGET_ROOT/etc/machine-id"

    if [[ ! -s "$TARGET_ROOT/etc/machine-id" ]]; then
        error "Failed to create /etc/machine-id."
        return 1
    fi

    # Without systemd, D-Bus may still read /var/lib/dbus/machine-id.
    # Symlink it to /etc/machine-id for a single source of truth.
    local dbus_dir="$TARGET_ROOT/var/lib/dbus"

    mkdir -p "$dbus_dir"

    # Remove any stale file or broken symlink that may exist
    # from the snapshot.
    rm -f "$dbus_dir/machine-id"

    if ! ln -s /etc/machine-id "$dbus_dir/machine-id"; then
        error "Failed to create /var/lib/dbus/machine-id symlink."
        return 1
    fi

    success "Machine-id generated: $machine_id"

    return 0
}

############################
generate_fstab() {
    local esp="$1"
    local root="$2"
    local swap="$3"
    local home="$4"

    local root_uuid
    local swap_uuid
    local home_uuid
    local esp_uuid

    if [[ -z "$TARGET_ROOT" || ! -d "$TARGET_ROOT" ]]; then
        error "Invalid target root: $TARGET_ROOT"
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
        if [[ -z "$esp" || ! -b "$esp" ]]; then
            error "Invalid EFI partition: $esp"
            return 1
        fi
    elif [[ "$FIRMWARE_MODE" != "bios" ]]; then
        error "Unknown firmware mode: $FIRMWARE_MODE"
        return 1
    fi

    root_uuid="$(blkid -s UUID -o value "$root")" || {
        error "Unable to read root filesystem UUID."
        return 1
    }

    swap_uuid="$(blkid -s UUID -o value "$swap")" || {
        error "Unable to read swap UUID."
        return 1
    }

    home_uuid="$(blkid -s UUID -o value "$home")" || {
        error "Unable to read home filesystem UUID."
        return 1
    }

    if [[ -z "$root_uuid" || -z "$swap_uuid" || -z "$home_uuid" ]]; then
        error "One or more required filesystem UUIDs are missing."
        return 1
    fi

    if [[ "$FIRMWARE_MODE" == "uefi" ]]; then
        esp_uuid="$(blkid -s UUID -o value "$esp")" || {
            error "Unable to read EFI filesystem UUID."
            return 1
        }

        if [[ -z "$esp_uuid" ]]; then
            error "EFI filesystem UUID is missing."
            return 1
        fi
    fi

    info "Generating /etc/fstab..."

    {
        printf '%s\n' '# /etc/fstab'
        printf '%s\n' '# Generated by BCALCOS Linux Installer.'
        printf '%s\n' '#'
        printf '%s\n' '# Filesystems are identified by UUID.'

        printf 'UUID=%s  /          ext4  defaults,errors=remount-ro  0 1\n' "$root_uuid"
        printf 'UUID=%s  none       swap  sw        0 0\n' "$swap_uuid"
        printf 'UUID=%s  /home      ext4  defaults  0 2\n' "$home_uuid"

        if [[ "$FIRMWARE_MODE" == "uefi" ]]; then
            printf 'UUID=%s  /boot/efi  vfat  defaults  0 1\n' "$esp_uuid"
        fi
    } > "$TARGET_ROOT/etc/fstab"

    if [[ ! -s "$TARGET_ROOT/etc/fstab" ]]; then
        error "Failed to create /etc/fstab."
        return 1
    fi

    success "Generated $TARGET_ROOT/etc/fstab."

    return 0
}
######################
install_grub_bios() {
    local disk="$1"

    if [[ "$FIRMWARE_MODE" != "bios" ]]; then
        error "GRUB BIOS installation requested while firmware mode is '$FIRMWARE_MODE'."
        return 1
    fi

    if [[ -z "$TARGET_ROOT" || ! -d "$TARGET_ROOT" ]]; then
        error "Target root is not mounted."
        return 1
    fi

    if [[ -z "$disk" || ! -b "$disk" ]]; then
        error "Invalid target disk: $disk"
        return 1
    fi

    if ! command_exists grub-install; then
        error "grub-install is required."
        return 1
    fi

    info "Installing GRUB for BIOS..."
    info "Target disk: $disk"

    # Verify BIOS Boot partition has correct type
    if ! verify_bios_boot_partition "$disk"; then
        return 1
    fi

    if ! grub-install \
        --target=i386-pc \
        --boot-directory="$TARGET_ROOT/boot" \
        "$disk"; then
        error "GRUB BIOS installation failed."
        return 1
    fi

    success "GRUB BIOS installation completed."

    return 0
}


install_grub_uefi() {
    local disk="$1"

    if [[ "$FIRMWARE_MODE" != "uefi" ]]; then
        error "GRUB UEFI installation requested while firmware mode is '$FIRMWARE_MODE'."
        return 1
    fi

    if [[ -z "$TARGET_ROOT" || ! -d "$TARGET_ROOT" ]]; then
        error "Target root is not mounted."
        return 1
    fi

    if [[ -z "$TARGET_EFI" || ! -d "$TARGET_EFI" ]]; then
        error "EFI System Partition is not mounted."
        return 1
    fi

    if [[ -z "$disk" || ! -b "$disk" ]]; then
        error "Invalid target disk: $disk"
        return 1
    fi

    if ! command_exists grub-install; then
        error "grub-install is required."
        return 1
    fi

    info "Installing GRUB for UEFI..."
    info "Target disk: $disk"
    info "EFI directory: $TARGET_EFI"

    if ! grub-install \
        --target=x86_64-efi \
        --efi-directory="$TARGET_EFI" \
        --boot-directory="$TARGET_ROOT/boot" \
        --removable \
        "$disk"; then
        error "GRUB UEFI installation failed."
        return 1
    fi

    # Verify UEFI fallback bootloader exists at expected path
    local fallback_efi="$TARGET_EFI/EFI/BOOT/BOOTX64.EFI"
    if [[ ! -f "$fallback_efi" ]]; then
        error "UEFI fallback bootloader not found: $fallback_efi"
        return 1
    fi

    success "GRUB UEFI installation completed."

    return 0
}



####################################
write_grub_stubs() {
    local dev
    local partnum
    local root_uuid

    if [[ -z "$TARGET_ROOT" || ! -d "$TARGET_ROOT" ]]; then
        error "Target root is not mounted."
        return 1
    fi

    if [[ -z "$TARGET_EFI" || ! -d "$TARGET_EFI" ]]; then
        error "EFI System Partition is not mounted."
        return 1
    fi

    if [[ -z "$TARGET_ROOT_PARTITION" || ! -b "$TARGET_ROOT_PARTITION" ]]; then
        error "Invalid root partition: $TARGET_ROOT_PARTITION"
        return 1
    fi

    # The installer always creates the root filesystem as GPT partition 2.
    # Verify that the supplied root device is in fact partition 2.
    dev="$(basename "$TARGET_ROOT_PARTITION")"

    if [[ ! -f "/sys/class/block/$dev/partition" ]]; then
        error "Root device is not a direct disk partition: $TARGET_ROOT_PARTITION"
        return 1
    fi

    partnum="$(< "/sys/class/block/$dev/partition")"

    if [[ "$partnum" != "2" ]]; then
        error "Unexpected root partition number: $partnum"
        error "This installer requires the root filesystem to be GPT partition 2."
        return 1
    fi

    # The stub locates the root filesystem by UUID, so it is independent
    # of GRUB's disk enumeration order (hd0, hd1, ...).
    root_uuid="$(blkid -s UUID -o value "$TARGET_ROOT_PARTITION")" || {
        error "Unable to read root filesystem UUID."
        return 1
    }

    if [[ -z "$root_uuid" ]]; then
        error "Root filesystem UUID is missing."
        return 1
    fi

    info "Writing GRUB stubs for root UUID $root_uuid ..."

    mkdir -p "$TARGET_EFI/EFI/BOOT" "$TARGET_EFI/boot/grub"

    {
        printf 'search --no-floppy --fs-uuid --set=root %s\n' "$root_uuid"
        printf 'set prefix=($root)/boot/grub\n'
        printf 'insmod normal\n'
        printf 'normal\n'
    } > "$TARGET_EFI/EFI/BOOT/grub.cfg" || {
        error "Failed to write $TARGET_EFI/EFI/BOOT/grub.cfg"
        return 1
    }

    if ! cp -f "$TARGET_EFI/EFI/BOOT/grub.cfg" \
        "$TARGET_EFI/boot/grub/grub.cfg"; then
        error "Failed to write boot-path GRUB stub."
        return 1
    fi

    if [[ ! -s "$TARGET_EFI/EFI/BOOT/grub.cfg" ||
          ! -s "$TARGET_EFI/boot/grub/grub.cfg" ]]; then
        error "GRUB stub verification failed."
        return 1
    fi

    success "GRUB stubs written for root UUID $root_uuid (both ESP paths)."

    return 0
}
####################################
unmount_chroot_mounts() {
    umount -R "$TARGET_ROOT/sys" 2>/dev/null || true
    umount "$TARGET_ROOT/proc" 2>/dev/null || true
    umount -R "$TARGET_ROOT/dev" 2>/dev/null || true
}

######################################
generate_grub_config() {
    if [[ -z "$TARGET_ROOT" || ! -d "$TARGET_ROOT" ]]; then
        error "Target root is not mounted."
        return 1
    fi

    if ! command_exists grub-mkconfig; then
        error "grub-mkconfig is required."
        return 1
    fi

    info "Preparing installed system for GRUB configuration..."

    if ! mount --rbind /dev "$TARGET_ROOT/dev"; then
        error "Failed to bind /dev into installed system."
        return 1
    fi

    if ! mount --make-rslave "$TARGET_ROOT/dev"; then
        error "Failed to configure /dev mount propagation."
        unmount_chroot_mounts
        return 1
    fi

    if ! mount -t proc proc "$TARGET_ROOT/proc"; then
        error "Failed to mount /proc in installed system."
        unmount_chroot_mounts
        return 1
    fi

    if ! mount --rbind /sys "$TARGET_ROOT/sys"; then
        error "Failed to bind /sys into installed system."
        unmount_chroot_mounts
        return 1
    fi

    if ! mount --make-rslave "$TARGET_ROOT/sys"; then
        error "Failed to configure /sys mount propagation."
        unmount_chroot_mounts
        return 1
    fi

    info "Generating GRUB configuration from installed target..."

    # Verify kernel and initramfs exist before generating GRUB config
    if ! ls "$TARGET_ROOT"/boot/vmlinuz-* >/dev/null 2>&1; then
        error "No kernel found in target /boot."
        unmount_chroot_mounts
        return 1
    fi

    if ! ls "$TARGET_ROOT"/boot/initrd.img-* >/dev/null 2>&1; then
        error "No initramfs found in target /boot."
        unmount_chroot_mounts
        return 1
    fi

    if ! chroot "$TARGET_ROOT" /usr/sbin/grub-mkconfig \
        -o /boot/grub/grub.cfg; then
        error "Failed to generate GRUB configuration."
        unmount_chroot_mounts
        return 1
    fi

    # Rebuild initramfs to embed the resume parameter
    info "Rebuilding initramfs for hibernation support..."

    if ! chroot "$TARGET_ROOT" /usr/sbin/update-initramfs -k all -u; then
        error "Failed to rebuild initramfs."
        unmount_chroot_mounts
        return 1
    fi

    unmount_chroot_mounts

    if [[ ! -s "$TARGET_ROOT/boot/grub/grub.cfg" ]]; then
        error "GRUB configuration was not created."
        return 1
    fi

    success "GRUB configuration generated."

    return 0
}

#########################
install_grub_branding() {
    local snapshot="$1"
    local iso_root
    local source_grub
    local target_grub

    if [[ -z "$TARGET_ROOT" || ! -d "$TARGET_ROOT" ]]; then
        error "Target root is not mounted."
        return 1
    fi

    if [[ -z "$snapshot" || ! -f "$snapshot" ]]; then
        error "Invalid snapshot path: $snapshot"
        return 1
    fi

    iso_root="$(dirname "$(dirname "$snapshot")")"
    source_grub="$iso_root/boot/grub"
    target_grub="$TARGET_ROOT/boot/grub"

    if [[ ! -d "$source_grub" ]]; then
        error "ISO GRUB directory not found: $source_grub"
        return 1
    fi

    mkdir -p "$target_grub"

    if [[ -f "$source_grub/splash.png" ]]; then
        cp -f "$source_grub/splash.png" "$target_grub/splash.png"
    else
        error "ISO GRUB splash image not found."
        return 1
    fi

    if [[ -f "$source_grub/font.pf2" ]]; then
        cp -f "$source_grub/font.pf2" "$target_grub/font.pf2"
    fi

    success "ISO GRUB branding installed."

    return 0
}

