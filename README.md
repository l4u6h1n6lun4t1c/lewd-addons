# LEWD Addon Manager

A small plugin for **Mod Organizer 2** that downloads the optional parts of LEWD mods for S.T.A.L.K.E.R. Anomaly and
G.A.M.M.A.

## What it is

LEWD mods stay small. The extras that not everybody wants are separate downloads:

- camos and weapon skins
- baked 3D scopes
- HD inventory icons
- LEWD ASS modules: scopes, bipods, lights, muzzle devices and more

The Addon Manager fetches the ones you pick from this page. Each one is installed as its own mod in MO2, right below
the mod it belongs to, and already ticked. You install the plugin **once**; every LEWD mod after that uses it, and it
updates itself.

## Install (one time)

1. Download **LEWD_Addon_Manager.zip** from the [latest release](../../releases/latest).
2. Unpack it, ideally inside your Mod Organizer folder (the one with `ModOrganizer.exe`, e.g. your GAMMA folder).
3. Close Mod Organizer and double-click **Install LEWD Addon Manager.bat**.
   If the zip is somewhere else, it asks you to pick the Mod Organizer folder.
4. Start Mod Organizer. Done.

By hand instead: put `lewd_addon_manager.py` into the `plugins` folder next to `ModOrganizer.exe` and restart MO2.

## How you use it

- **In a LEWD mod's installer**, options marked DOWNLOAD are fetched right after the installer finishes.
- **Tools > LEWD Addons** shows everything: what you have, what is new, what you can add.
- **When MO2 starts**, it tells you when something you have got a newer version (for example new scopes). Close the
  window and it stays quiet about that version.

Every download is checked (SHA-256) before it is installed. Each addon zip in the releases is also a normal mod
archive, so you can install it by hand like any mod, below the mod it belongs to.

Settings: MO2 > Settings > Plugins > LEWD Addon Manager (update notices, HD icons, offer installer picks).

## Remove it

Delete `plugins\lewd_addon_manager.py` in your Mod Organizer folder. Addons you installed stay as normal mods.

## For mod authors

The addon list is `index.json` in the latest release. Each LEWD mod carries `lewd_addon.json` (`id`, `version`) at its
root; installer options that need a download drop an empty marker `lewd_addons/want/<addon id>`.
