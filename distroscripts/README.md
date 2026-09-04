# Readme Contents

1. [Introduction](#1-introduction)
2. [Update Notification](#2-update-notification)
3. [Fstrim](#3-fstrim)
4. [Marp GUI](#4-marp-gui)

---

## 1. Introduction

A few simple custom scripts included with BcalcOS Linux. 

---

## 2. Update Notification

Location:  /usr/sbin/apt-updates-notify

Daily check around 4pm for APT updates.

Displays desktop notification only; does not install updates.

---

## 3. Fstrim 

Location:  /usr/sbin/weekly-fstrim

Weekly disk maintenance to help SSDs maintain performance; does not delete files.

Sends TRIM commands to all mounted filesystems that support them.

---

## 4. Marp GUI

Name:  Marp
Location:  /usr/bin/marp-pick

Graphical launcher for converting a Marp markdown file into a PDF file.

Can be run via applications menu under Office.

---

