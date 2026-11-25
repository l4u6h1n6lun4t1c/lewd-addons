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

## LEWD Modded Exes

The Addon Manager also installs and updates the **LEWD Modded Exes**, the LEWD fork of the Modded Exes
([lewd-modded-exe](https://github.com/l4u6h1n6lun4t1c/lewd-modded-exe)). It works on plain Anomaly and on GAMMA.

- Open **Tools > LEWD Addons > LEWD Modded Exes...** to install, update, switch back to the stock exes or roll back.
  MO2 also asks once on start, and again whenever a new build is out.
- The exes go into a separate `bin_lewd` folder next to `bin`. Your `bin` folder is never changed, so GAMMA updates
  can't break it and the stock exes stay as a fallback.
- Their gamedata is the mod **Modded Exes - LEWD**, at the lowest priority.
- Start the game from MO2 as usual: when MO2 starts an exe from `bin`, the plugin starts the same exe from `bin_lewd`.
- After the first install, clear the shader cache once in the Anomaly launcher.
- A LEWD mod that needs the LEWD exes can ask for them like an addon: its installer drops the marker
  `lewd_addons/want/lewd.exes`.

## Remove it

Delete `plugins\lewd_addon_manager.py` in your Mod Organizer folder. Addons you installed stay as normal mods.

## For mod authors

The addon list is `index.json` in the latest release. Each LEWD mod carries `lewd_addon.json` (`id`, `version`) at its
root; installer options that need a download drop an empty marker `lewd_addons/want/<addon id>`.
