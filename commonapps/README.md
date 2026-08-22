# Readme Contents

1. [Introduction](#1-introduction)
2. [gocryptfs](#2-gocryptfs)
3. [ClamAV](#3-clamav)
4. [UFW](#4-ufw)

---

## 1. Introduction

Basic instructions for several applications.

---

## 2. gocryptfs

Name:  gocryptfs

Functionality: file-based encryption tool

Suggested folder structure
- /home/crypt
- /home/crypt/encrypt
- /home/crypt/decrypt (where you place your files)

Essential Commands:
- gocryptfs -init ~/crypt/encrypt (just once, set password) 
- gocryptfs ~/crypt/encrypt ~/crypt/decrypt (mount)
- fusermount -u /home/crypt/decrypt (unmount)

may want to add to .bash_aliases so can use

crypt mount and crypt unmount rather than full commands

crypt ( ) { 
    case "$1" in
        mount)
            gocryptfs ~/crypt/encrypt ~/crypt/decrypt
            ;;
        umount)
            fusermount -u ~/crpt/decrypt
            ;;
        *)
            echo "Usage: crypt {mount|unmount}"
            ;;
    esac
}

---

## 3. ClamAV

Name:  Clam AntiVirus

Functionality: cross-platform antimalware toolkit; relies upon signature-based detection

Essential Commands:
- sudo freshclam to manually download latest definitions
- sudo clamscan -r -i ~ to scan your home directory and provide a summary when finished

---

## 4. UFW

Name:  Uncomplicated Firewall

Functionality: simplify the configuration of Linux host-based firewalls; generates the underlying iptables/nftables rules

Essential Commands:
- sudo ufw status
- sudo ufw enable 

---

