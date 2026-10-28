# Readme Contents

1. [Introduction](#1-introduction)
2. [Distro Installer](#2-distro-installer)
3. [Welcome Center](#3-welcome-center)
4. [Quail Calc](#4-quail-calc)
5. [Quail Reminder](#5-quail-reminder)
6. [Quail System](#6-quail-system)
7. [Quail Converter](#7-quail-converter)
8. [Quail Scan](#8-quail-scan)
---

## 1. Introduction

BcalcOS custom applications.

---

## 2. Distro Installer

Name:  Install BcalcOS

Location: /usr/bin/bcalcos-installer/

BcalcOS versions 1.4 and later:  GUI installer.  Terminal installer retired.
BcalcOS versions 1.3.5 and earlier: terminal based distribution installer.

Under-the-hood remains the same:
- automatically determines boot method, partition structure, partition sizes, language, and keyboard
- user selects disk, timezone, user name, /home partition encryption, and password(s)

Only run once to install distribution.<br>
Warning:  existing data on the drive will be erased.  

---

## 3. Welcome Center

Name: bcalcos_welcome

Location:  /usr/bin/bcalcos_welcome

GUI application to assist user with installing or removing curated applications, performing basic system functions, and locating resource guides.

Can be run at user's discretion via application menu under BcalcOS category.

---

## 4. Quail Calc 

Name:  quail_calc

Location:  /usr/bin/quail_calc

Simple GUI calculator.

Can be run via <kbd>Super</kbd> + <kbd>C</kbd>

---

## 5. Quail Reminder 

Name:  quail-reminder

Location:  /usr/bin/quail-reminder

Simple hours:minutes timer/ reminder.

Can be run via <kbd>Super</kbd> + <kbd>B</kbd>

---

## 6. Quail System 

Name:  quail-system

Location:  /usr/bin/quail-system

Provides an overview of your computer and BcalcOS installation.

Can be run via application menu under BcalcOS category.

---

## 7. Quail Converter 

Name:  quail-converter

Location:  /usr/bin/quail-converter

Simple app for converting between common units of measurement.

Can be run via application menu under BcalcOS category.

---

## 8. Quail Scan 

Name:  quail-scan

Location:  /usr/bin/quail-scan

GUI interface for ClamAV.

Prerequisite:  perhaps not surprising, ClamAV should be installed first.

Can be run via application menu under BcalcOS category.

---