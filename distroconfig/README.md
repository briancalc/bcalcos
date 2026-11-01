# Readme Contents
# v1.3

1. [Introduction](#1-introduction)
2. [Font Hide](#2-font-hide)
3. [Branding Preference](#2-branding-preference)

---

## 1. Introduction

A simple custom font configuration included with BcalcOS Linux. 

---

## 2. Font Hide

Location:  /etc/fonts/local.conf

Base Devuan is pretty light on fonts but there were still a few too many. 

To restore a font: simply erase the appropriate row.

To completely remove the file: use sudo rm /etc/fonts/local.conf

Then rebuild the font cache: sudo fc-cache -f -v

---

## 3. Branding Preference

Location:  /etc/apt/preferences.d/

Pins Devuan's `desktop-base` to `1:5.9`

Devuan uses desktop-base to roll out its release themes and Devuan branding.

Those updates could overwrite custom BcalcOS branding with Devuan's theme, icons, wallpapers, and backgrounds.

This is a version pin so upgrades are no longer received.  Use `apt install --reinstall desktop-base` to restore.

---

