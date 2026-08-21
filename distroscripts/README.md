# Readme Contents

1. [Introduction](#1-introduction)
2. [Font Hide](#2-font-hide)
3. [Update Notification](#3-update-notification)
4. [Fstrim](#4-fstrim)
5. [Marp GUI](#4-marp-gui)

---

## 1. Introduction

A few simple custom scripts included with BcalcOS Linux. 

---

## 2. Font Hide

Location:  /etc/fonts/local.conf

Base Devuan is pretty light on fonts but there were still a few too many. 

To restore a font: simply erase the approporate row.
To completely remove the file: use sudo rm /etc/fonts/local.conf

Then rebuild the font cache: sudo fc-cache -f -v

---

## 3. Update Notification

Location:  /usr/local/sbin/apt-updates-notify

Daily check around 4pm for APT or Marp updates.

Displays desktop notification only; does not install updates.

---

## 4. Fstrim 

Location:  /usr/local/sbin/weekly-fstrim

Weekly disk maintenance to help SSDs maintain performance; does not delete files.

Sends TRIM commands to all mounted filesystems that support them.

---

## 5. Marp GUI

Name:  Marp
Location:  /usr/local/bin/marp-pick.py

Graphical launcher for converting a Marp markdown file into a PDF file.

Can be run via applications menu under Office.

---

