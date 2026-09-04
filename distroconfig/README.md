# Readme Contents

1. [Introduction](#1-introduction)
2. [Font Hide](#2-font-hide)

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

