# LEWD Addons

Optional downloads for LEWD mods for S.T.A.L.K.E.R. Anomaly / G.A.M.M.A. (camos, baked scopes, HQ textures,
attachments, icons). The base mods stay small; you pick the extras.

## How to get them

**With the LEWD Addon Manager (MO2 plugin, recommended)**
1. Download `lewd_addon_manager.py` from the latest release.
2. Put it into the `plugins` folder next to `ModOrganizer.exe`.
3. Restart MO2. Open **Tools -> LEWD Addons**, tick what you want, click install.
   Addons you ticked in a LEWD mod's installer are offered automatically.

Each addon is installed as its own mod right below the mod it belongs to, already enabled.

**By hand**
Every addon zip in the releases is a normal mod archive: install it in MO2 and place it below its base mod.

## For mod authors
The addon list is `index.json` in the latest release. Each LEWD mod carries `lewd_addon.json` (`id`, `version`)
at its root; installer options that need a download drop an empty marker `lewd_addons/want/<addon id>`.
