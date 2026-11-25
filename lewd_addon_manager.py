"""LEWD Addon Manager - MO2 plugin.

Lean LEWD mods (e.g. the Croatia Pack) ship only the guns. Extras (camos, baked scopes, HQ textures, LEWD ASS parts,
icons...) are separate zips on GitHub Releases, listed in one manifest (index.json). This plugin:
  - reads the manifest (setting "manifest_url"; https:// or file://)
  - finds installed LEWD mods by the lewd_addon.json at their mod root (fallback: the manifest's "detect" rule)
  - downloads the picked addons + their requirements, checks sha256, installs each as its own MO2 mod right after
    the mod it belongs to, and enables it
  - after a FOMOD install, picks up the addons the player ticked: the FOMOD drops empty marker files
    <mod>/lewd_addons/want/<addon id> and the plugin offers to download those.

Tools menu -> "LEWD Addons". Install: copy this file into <MO2>/plugins/ and restart MO2.

Manifest (index.json):
{
  "schema": 1,
  "plugin": {"version": "0.1.0", "url": "...lewd_addon_manager.py", "sha256": "..."},
  "addons": [
    {"id": "croatia.camos", "name": "Camos", "pack": "LEWD Croatia Pack", "version": "2.1",
     "mod_name": "LEWD Croatia Pack - Camos", "url": "https://.../croatia.camos-2.1.zip", "sha256": "...",
     "size": 812345678, "description": "...",
     "requires": [{"id": "croatia.base", "min": "2.1"}, {"id": "lewd.camo", "min": "1.0"}],
     "after": "croatia.base",
     "detect": {"file": "gamedata/configs/..."}           (optional, for mods without lewd_addon.json)
    }, ...]
}
An entry without "url" is listed for detection/requirements only (e.g. a mod published elsewhere: "page" link).

LEWD Modded Exes (0.4.0): the engine fork from github.com/l4u6h1n6lun4t1c/lewd-modded-exe. Its releases carry
lewd-exes.json {version, zip, files: {relative path: sha256}}. The plugin installs the exes into <game>/bin_lewd
(never into bin/), the release's db/ gamedata into the mod "Modded Exes - LEWD" at the lowest priority, keeps the
previous build as bin_lewd.prev for rollback, and starts bin_lewd/X.exe whenever MO2 is about to start bin/X.exe.
"""
import hashlib
import json
import os
import re
import subprocess
import shutil
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

__version__ = "0.4.0"
DEFAULT_MANIFEST = "https://github.com/l4u6h1n6lun4t1c/lewd-addons/releases/latest/download/index.json"
EXES_MANIFEST = "https://github.com/l4u6h1n6lun4t1c/lewd-modded-exe/releases/latest/download/lewd-exes.json"
EXES_BIN = "bin_lewd"
EXES_MOD = "Modded Exes - LEWD"
ID_FILE = "lewd_addon.json"
WANT_DIR = ("lewd_addons", "want")
UA = "LEWD-Addon-Manager/" + __version__


# ---------------------------------------------------------------- core (no MO2 needed, testable on its own)

def vtuple(v):
    out = []
    for part in str(v or "0").replace("-", ".").split("."):
        num = "".join(ch for ch in part if ch.isdigit())
        out.append(int(num) if num else 0)
    while len(out) > 1 and out[-1] == 0:
        out.pop()
    return tuple(out)


_RELEASE_URL = re.compile(r"^https://github\.com/([^/]+/[^/]+)/releases/(?:download/([^/]+)|latest/download)/([^/?#]+)$")
_token = []


def github_token():
    """Token for PRIVATE release downloads (author's own PC): GH_TOKEN / GITHUB_TOKEN, else `gh auth token`.
    Players of a public repo need none; without a token everything goes the anonymous way."""
    if not _token:
        tok = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
        if not tok:
            for gh in ("gh", r"C:\Program Files\GitHub CLI\gh.exe"):
                try:
                    tok = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, timeout=10,
                                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()
                except Exception:
                    tok = ""
                if tok:
                    break
        _token.append(tok)
    return _token[0]


def open_url(url, timeout=60):
    """urlopen; a github release asset url goes through the API with the token when one exists (private repo)."""
    m = _RELEASE_URL.match(url)
    tok = github_token() if m else ""
    if not tok:
        return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=timeout)
    repo, tag, name = m.groups()
    api = "https://api.github.com/repos/%s/releases/%s" % (repo, ("tags/" + tag) if tag else "latest")
    hdr = {"User-Agent": UA, "Authorization": "Bearer " + tok, "Accept": "application/vnd.github+json"}
    with urllib.request.urlopen(urllib.request.Request(api, headers=hdr), timeout=timeout) as r:
        rel = json.loads(r.read().decode("utf-8"))
    asset = next((x for x in rel.get("assets", []) if x["name"] == name), None)
    if asset is None:
        raise FileNotFoundError("%s not in release %s" % (name, rel.get("tag_name")))
    hdr["Accept"] = "application/octet-stream"
    return urllib.request.urlopen(urllib.request.Request(asset["url"], headers=hdr), timeout=timeout)


def fetch_manifest(url, timeout=20):
    with open_url(url, timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    if data.get("schema") != 1:
        raise ValueError("unknown manifest schema %r" % data.get("schema"))
    return data


def read_meta_version(mod_dir):
    meta = mod_dir / "meta.ini"
    if not meta.is_file():
        return None
    for line in meta.read_text("utf-8", errors="replace").splitlines():
        if line.strip().lower().startswith("version="):
            return line.split("=", 1)[1].strip() or None
    return None


def scan_mods(mods_dir, manifest, active=None):
    """-> {addon id: {"mod": folder name, "version": str, "active": bool}} for every installed LEWD mod we can identify.
    active = set of enabled mod names. Enabled mods always win. A DISABLED mod only counts when no enabled mod has
    that id, and then only when it carries lewd_addon.json (a fresh install MO2 left unticked) - a disabled mod that
    merely matches a detect file is an old copy and is ignored (2026-10-13: old 'LEWD VHS-2' taken as the base).
    Several folders with the same id: the highest version wins."""
    found, sleeping = {}, {}
    detect = [(a["id"], a["detect"]) for a in manifest.get("addons", []) if a.get("detect")]
    for d in sorted(Path(mods_dir).iterdir()) if Path(mods_dir).is_dir() else []:
        if not d.is_dir() or d.name.endswith("_separator"):
            continue
        on = active is None or d.name in active
        hits = []
        idf = d / ID_FILE
        if idf.is_file():
            try:
                info = json.loads(idf.read_text("utf-8"))
                hits.append((info["id"], str(info.get("version", "0"))))
            except Exception:
                pass
        if on:
            for aid, rule in detect:
                f = rule.get("file")
                if f and (d / f).is_file() and aid not in [h[0] for h in hits]:
                    hits.append((aid, read_meta_version(d) or "0"))
        target = found if on else sleeping
        for aid, ver in hits:
            cur = target.get(aid)
            if cur is None or vtuple(ver) > vtuple(cur["version"]):
                target[aid] = {"mod": d.name, "version": ver, "active": on}
    for aid, v in sleeping.items():
        found.setdefault(aid, v)
    return found


def wanted_markers(mods_dir):
    """-> {addon id: [mod folder names that asked for it]} from the FOMOD marker files."""
    out = {}
    root = Path(mods_dir)
    if not root.is_dir():
        return out
    for d in root.iterdir():
        w = d.joinpath(*WANT_DIR)
        if w.is_dir():
            for f in w.iterdir():
                if f.is_file():
                    out.setdefault(f.name, []).append(d.name)
    return out


def _need(why, name, minv):
    v = "" if vtuple(minv) == (0,) else " %s or newer" % minv
    return ("%s needs %s%s." % (why, name, v)) if why else ("%s%s:" % (name, v))


def resolve(picked, manifest, installed):
    """Order the picked addon ids + every missing/outdated requirement, requirements first.
    -> (install list of addon dicts, problems list of str)"""
    by_id = {a["id"]: a for a in manifest.get("addons", [])}
    order, problems, ok = [], [], {}

    def need(aid, minv, why):
        have = installed.get(aid)
        if have and vtuple(have["version"]) >= vtuple(minv):
            return True
        return visit(aid, why, minv)

    def visit(aid, why, minv="0"):
        """True when aid will be there (queued); an addon whose requirement fails is skipped with a problem line."""
        if aid in ok:
            return ok[aid]
        ok[aid] = False
        a = by_id.get(aid)
        if a is None:
            problems.append(_need(why, "'%s'" % aid, minv) + " It is not in the addon list.")
            return False
        name = a.get("name", aid)
        if vtuple(a.get("version")) < vtuple(minv):
            problems.append(_need(why, name, minv) + " Newest available: %s." % a.get("version"))
            return False
        if not a.get("url"):
            where = (" Get it here: " + a["page"]) if a.get("page") else ""
            problems.append(_need(why, name, minv) + " It is not downloadable yet, install it by hand." + where)
            return False
        if not all([need(r["id"], r.get("min", "0"), name) for r in a.get("requires", [])]):
            problems.append("%s skipped." % name)
            return False
        order.append(a)
        ok[aid] = True
        return True

    for aid in picked:
        have, a = installed.get(aid), by_id.get(aid)
        if have and (a is None or vtuple(have["version"]) >= vtuple(a.get("version"))):
            continue  # already there and up to date
        visit(aid, None)
    return order, problems


def download(url, dest, sha256=None, progress=None, cancel=None):
    """Download to dest (.part first), verify sha256. progress(done, total) is called from this thread."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and sha256 and file_sha256(dest) == sha256.lower():
        return dest
    part = dest.with_name(dest.name + ".part")
    h = hashlib.sha256()
    with open_url(url) as r, open(part, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            if cancel and cancel.is_set():
                raise InterruptedError("cancelled")
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    if sha256 and h.hexdigest() != sha256.lower():
        part.unlink(missing_ok=True)
        raise ValueError("checksum mismatch for %s" % dest.name)
    os.replace(part, dest)
    return dest


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def zip_has_fomod(path):
    with zipfile.ZipFile(path) as z:
        return any(n.lower().replace("\\", "/").endswith("fomod/moduleconfig.xml") for n in z.namelist())


HD_DIR = "hd icons/"


def extract_mod(zip_path, mods_dir, addon, hd=True):
    """Plain addon zip (gamedata/... + lewd_addon.json) -> <mods>/<mod_name>, replacing an older copy.
    An optional "HD icons/gamedata/..." folder in the zip (LEWD ASS modules) is merged over gamedata when hd is on
    and skipped otherwise."""
    target = Path(mods_dir) / addon["mod_name"]
    tmp = target.with_name(target.name + ".lewdtmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    with zipfile.ZipFile(zip_path) as z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        for i in infos:
            p = Path(i.filename)
            if p.is_absolute() or ".." in p.parts:
                raise ValueError("unsafe path in zip: %s" % i.filename)
        hd_files = [i for i in infos if i.filename.replace("\\", "/").lower().startswith(HD_DIR)]
        for i in infos:
            if i not in hd_files:
                z.extract(i, tmp)
        if hd:
            for i in hd_files:
                rel = i.filename.replace("\\", "/")[len(HD_DIR):]
                dest = tmp / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                with z.open(i) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
    (tmp / ID_FILE).write_text(json.dumps({"id": addon["id"], "version": addon["version"]}, indent=1), "utf-8")
    (tmp / "meta.ini").write_text(
        "[General]\ngameName=stalkeranomaly\nmodid=0\nversion=%s\ninstallationFile=%s\nurl=%s\n"
        "comments=installed by LEWD Addon Manager\n" % (addon["version"], Path(zip_path).name, addon.get("url", "")),
        "utf-8")
    if target.exists():
        shutil.rmtree(target)
    os.replace(tmp, target)
    return target


# ---------------------------------------------------------------- LEWD Modded Exes (core, no MO2 needed)

def exes_installed(game_dir):
    """Version in <game>/bin_lewd/lewd-exes.json, or "" when not installed."""
    try:
        return json.loads((Path(game_dir) / EXES_BIN / "lewd-exes.json").read_text("utf-8")).get("version", "")
    except (OSError, ValueError):
        return ""


def fetch_exes_manifest(url, timeout=20):
    with open_url(url, timeout) as r:
        m = json.loads(r.read().decode("utf-8"))
    if not m.get("version") or not m.get("zip") or not m.get("files"):
        raise ValueError("lewd-exes.json is incomplete")
    return m


def exes_zip_url(manifest_url, manifest):
    """The release zip next to the manifest, pinned to the manifest's own release tag."""
    m = _RELEASE_URL.match(manifest_url)
    if m:
        return "https://github.com/%s/releases/download/%s/%s" % (m.group(1), manifest["version"], manifest["zip"])
    return manifest_url.rsplit("/", 1)[0] + "/" + manifest["zip"]


def exes_extract(zip_path, manifest, out_dir):
    """Unpack the release zip and check every file listed in the manifest (SHA-256)."""
    out_dir = Path(out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    with zipfile.ZipFile(zip_path) as z:
        for n in z.namelist():
            p = Path(n)
            if p.is_absolute() or ".." in p.parts:
                raise ValueError("unsafe path in zip: %s" % n)
        z.extractall(out_dir)
    for rel, digest in manifest["files"].items():
        f = out_dir.joinpath(*rel.split("/"))
        if not f.is_file():
            raise ValueError("missing from the zip: %s" % rel)
        if file_sha256(f) != digest.lower():
            raise ValueError("checksum mismatch: %s" % rel)
    return out_dir


def exes_install(game_dir, mods_dir, pkg, manifest):
    """bin_lewd.new = runtime files of bin/ (DLLs, ini) + our exes + manifest; swap it in (old -> bin_lewd.prev);
    db/ -> the "Modded Exes - LEWD" mod. bin/ itself is never written to."""
    game = Path(game_dir)
    cur, new, prev = game / EXES_BIN, game / (EXES_BIN + ".new"), game / (EXES_BIN + ".prev")
    if new.exists():
        shutil.rmtree(new)
    new.mkdir(parents=True)
    src_bin = game / "bin"
    if src_bin.is_dir():
        for f in src_bin.iterdir():
            if f.is_file() and f.suffix.lower() not in (".exe", ".pdb", ".log"):
                shutil.copy2(f, new / f.name)
    for f in (Path(pkg) / "bin").iterdir():
        shutil.copy2(f, new / f.name)
    (new / "lewd-exes.json").write_text(json.dumps(manifest, indent=1), "utf-8")
    if cur.exists():
        old_prev = game / (EXES_BIN + ".prev.old")
        if old_prev.exists():
            shutil.rmtree(old_prev)
        if prev.exists():
            os.replace(prev, old_prev)
        try:
            os.replace(cur, prev)
        except OSError:
            if old_prev.exists():
                os.replace(old_prev, prev)
            shutil.rmtree(new, ignore_errors=True)
            raise RuntimeError("bin_lewd is in use. Close the game and try again.")
        shutil.rmtree(old_prev, ignore_errors=True)
    os.replace(new, cur)
    mod = Path(mods_dir) / EXES_MOD
    mod.mkdir(parents=True, exist_ok=True)
    if (mod / "db").exists():
        shutil.rmtree(mod / "db")
    if (Path(pkg) / "db").is_dir():
        shutil.copytree(Path(pkg) / "db", mod / "db")
    (mod / "meta.ini").write_text(
        "[General]\ngameName=stalkeranomaly\nmodid=0\nversion=%s\ninstallationFile=%s\n"
        "comments=installed by LEWD Addon Manager (LEWD Modded Exes gamedata)\n" % (manifest["version"], manifest["zip"]),
        "utf-8")
    return manifest["version"]


def exes_rollback(game_dir):
    """Swap bin_lewd and bin_lewd.prev. -> the version now active."""
    game = Path(game_dir)
    cur, prev, tmp = game / EXES_BIN, game / (EXES_BIN + ".prev"), game / (EXES_BIN + ".swap")
    if not prev.is_dir():
        raise RuntimeError("There is no previous build to roll back to.")
    if tmp.exists():
        shutil.rmtree(tmp)
    try:
        if cur.exists():
            os.replace(cur, tmp)
        os.replace(prev, cur)
        if tmp.exists():
            os.replace(tmp, prev)
    except OSError:
        raise RuntimeError("bin_lewd is in use. Close the game and try again.")
    return exes_installed(game_dir)


def exes_redirect(game_dir, path):
    """bin/X.exe -> bin_lewd/X.exe when that exists, else None."""
    try:
        p = Path(path).resolve()
        if p.suffix.lower() != ".exe" or p.parent != (Path(game_dir) / "bin").resolve():
            return None
        cand = Path(game_dir) / EXES_BIN / p.name
        return cand if cand.is_file() else None
    except (OSError, ValueError, TypeError):
        return None


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%.0f %s" if unit in ("B", "KB") else "%.1f %s") % (n, unit)
        n /= 1024.0


# ---------------------------------------------------------------- MO2 side

HELP_HTML = """
<h3>LEWD Addon Manager</h3>
<p>LEWD mods stay small: the extras that not everybody wants (camos, baked scopes, HD icons, LEWD ASS modules such as
scopes, bipods and lights) are separate downloads. This plugin fetches them for you from GitHub and installs each one
as its own mod in MO2, right below the mod it belongs to, already ticked.</p>
<p><b>When does it show up?</b></p>
<ul>
<li>After you install a LEWD mod and ticked a DOWNLOAD option in its installer: it offers exactly those.</li>
<li>When MO2 starts and something you already have got a newer version (for example new scopes in the all-in
collection). Close the window and it stays quiet about that version.</li>
<li>Any time from <b>Tools &gt; LEWD Addons</b>: the full list, with what you have and what is new.</li>
</ul>
<p><b>Good to know</b></p>
<ul>
<li>Requirements are added on their own (for example a camo pack needs its gun mod).</li>
<li>Every download is checked (SHA-256) before it is installed. Files land in MO2's downloads folder under
"LEWD addons", so you can reinstall without downloading again.</li>
<li>Every addon zip is a normal mod archive: you can also install it by hand from the GitHub release page.</li>
<li>To remove an addon, delete its mod in MO2 like any other.</li>
<li>Settings (MO2 &gt; Settings &gt; Plugins &gt; LEWD Addon Manager): update notices, HD icons, offer installer picks.</li>
</ul>
<p><b>LEWD Modded Exes</b> (button in this window): the LEWD engine build. It is installed into its own
<code>bin_lewd</code> folder next to <code>bin</code>, which stays untouched (so GAMMA updates can't break it), and MO2
starts it whenever you start the game as usual. Its gamedata is the mod "Modded Exes - LEWD" at the top of the list
(lowest priority). You can switch back to the stock exes or roll back to the previous build in the same window.</p>
<p>The plugin updates itself when a new version is out (you are asked first). Apart from <code>bin_lewd</code> it never
touches your game folder, and it never changes a mod that is not a LEWD addon.</p>
"""


try:
    import mobase
    from PyQt6.QtCore import Qt, QTimer
    from PyQt6.QtGui import QIcon
    from PyQt6.QtWidgets import (QDialog, QDialogButtonBox, QLabel, QMessageBox, QProgressDialog, QTreeWidget,
                                 QTreeWidgetItem, QVBoxLayout)
except ImportError:  # running the core tests outside MO2
    mobase = None

if mobase is not None:

    class AddonDialog(QDialog):
        def __init__(self, parent, manifest, installed, preselect=(), intro=None, exes_cb=None):
            super().__init__(parent)
            self.setWindowTitle("LEWD Addons")
            self.resize(820, 520)
            lay = QVBoxLayout(self)
            lab = QLabel(intro or "Tick the addons to install or update. Requirements are added automatically.")
            lab.setWordWrap(True)
            lay.addWidget(lab)
            self.tree = QTreeWidget()
            self.tree.setHeaderLabels(["Addon", "Size", "Installed", "Available", "Notes"])
            self.tree.setToolTip("Hover an addon for its description. Tick it to install or update.")
            self.tree.setColumnWidth(0, 330)
            packs = {}
            for a in manifest.get("addons", []):
                if not a.get("url") or a.get("hidden"):
                    continue
                pack = packs.get(a.get("pack", "Other"))
                if pack is None:
                    pack = packs[a.get("pack", "Other")] = QTreeWidgetItem(self.tree, [a.get("pack", "Other")])
                    pack.setExpanded(True)
                have = installed.get(a["id"])
                hv = have["version"] if have else ""
                state = "" if not have else ("up to date" if vtuple(hv) >= vtuple(a["version"]) else "update")
                it = QTreeWidgetItem(pack, [a.get("name", a["id"]), human(a.get("size", 0)), hv, a["version"], state])
                it.setToolTip(0, a.get("description", ""))
                it.setToolTip(4, a.get("description", ""))
                it.setData(0, Qt.ItemDataRole.UserRole, a["id"])
                tick = a["id"] in preselect or state == "update"
                it.setCheckState(0, Qt.CheckState.Checked if tick else Qt.CheckState.Unchecked)
            lay.addWidget(self.tree)
            bb = QDialogButtonBox()
            help_btn = bb.addButton("What is this?", QDialogButtonBox.ButtonRole.HelpRole)
            help_btn.setToolTip("What the LEWD Addon Manager does and when it shows up")
            help_btn.clicked.connect(lambda: QMessageBox.information(self, "LEWD Addon Manager", HELP_HTML))
            if exes_cb is not None:
                exes_btn = bb.addButton("LEWD Modded Exes...", QDialogButtonBox.ButtonRole.ActionRole)
                exes_btn.setToolTip("Install or update the LEWD engine build, switch to the stock exes or roll back")
                exes_btn.clicked.connect(lambda: (self.reject(), exes_cb()))
            ok_btn = bb.addButton("Install / update ticked", QDialogButtonBox.ButtonRole.AcceptRole)
            ok_btn.setToolTip("Download the ticked addons (and anything they need), check them and install each as "
                              "its own mod, below the mod it belongs to")
            bb.addButton(QDialogButtonBox.StandardButton.Close)
            bb.accepted.connect(self.accept)
            bb.rejected.connect(self.reject)
            lay.addWidget(bb)

        def picked(self):
            out = []
            root = self.tree.invisibleRootItem()
            for i in range(root.childCount()):
                pack = root.child(i)
                for j in range(pack.childCount()):
                    it = pack.child(j)
                    if it.checkState(0) == Qt.CheckState.Checked:
                        out.append(it.data(0, Qt.ItemDataRole.UserRole))
            return out

    class LewdAddonManager(mobase.IPluginTool):
        def __init__(self):
            super().__init__()
            self._org = None
            self._parent = None
            self._manifest = None

        # --- plugin boilerplate
        def init(self, organizer):
            self._org = organizer
            organizer.onUserInterfaceInitialized(lambda win: (QTimer.singleShot(3000, lambda: self._check_markers(True)),
                                                              QTimer.singleShot(6000, lambda: self._check_exes(False))))
            organizer.onAboutToRun(self._on_about_to_run)
            ml = organizer.modList()
            if hasattr(ml, "onModInstalled"):
                ml.onModInstalled(lambda mod: QTimer.singleShot(1500, self._check_markers))
            return True

        def name(self):
            return "LEWD Addon Manager"

        def author(self):
            return "LEWD"

        def description(self):
            return ("Downloads and installs the optional parts of LEWD mods (camos, baked scopes, HD icons, LEWD ASS "
                    "modules) from GitHub, each as its own mod. Offers what you ticked in a LEWD installer and tells you "
                    "about new versions. Open it from Tools > LEWD Addons.")

        def version(self):
            p = [int(x) for x in __version__.split(".")]
            return mobase.VersionInfo(p[0], p[1], p[2], mobase.ReleaseType.FINAL)

        def settings(self):
            return [mobase.PluginSetting("manifest_url", "Addon list URL (https:// or file:///)", DEFAULT_MANIFEST),
                    mobase.PluginSetting("ask_on_start", "Offer addons picked in a FOMOD", True),
                    mobase.PluginSetting("check_updates", "Tell me about new addon versions when MO2 starts", True),
                    mobase.PluginSetting("hd_icons", "Install the HD icons that come with addons", True),
                    mobase.PluginSetting("exes_updates", "Offer new LEWD Modded Exes when MO2 starts", True),
                    mobase.PluginSetting("use_lewd_exes", "Start the LEWD Modded Exes (bin_lewd) in place of the "
                                         "stock exes in bin", True),
                    mobase.PluginSetting("exes_manifest_url", "LEWD Modded Exes release info URL", EXES_MANIFEST)]

        def displayName(self):
            return "LEWD Addons"

        def tooltip(self):
            return ("Download optional parts of LEWD mods (camos, scopes, HD icons, LEWD ASS modules) "
                    "and see what is new")

        def icon(self):
            return QIcon()

        def setParentWidget(self, widget):
            self._parent = widget

        # --- helpers
        def _mods_dir(self):
            return Path(self._org.modsPath())

        def _active(self):
            ml = self._org.modList()
            return {m for m in ml.allMods() if ml.state(m) & mobase.ModState.ACTIVE}

        def _load_manifest(self):
            url = self._org.pluginSetting(self.name(), "manifest_url") or DEFAULT_MANIFEST
            override = Path(__file__).with_suffix(".url")  # dev/test: lewd_addon_manager.url next to the plugin
            if url == DEFAULT_MANIFEST and override.is_file():
                url = override.read_text("utf-8").strip() or url
            self._manifest = fetch_manifest(url)
            return self._manifest

        def _check_markers(self, startup=False):
            """Load the addon list in the background (MO2 stays responsive), then offer
            (a) addons a FOMOD asked for that are not installed, (b) newer versions of installed addons
            (e.g. new scopes in a growing collection). Declined offers are remembered per id@version."""
            wants = wanted_markers(self._mods_dir()) if self._org.pluginSetting(self.name(), "ask_on_start") else {}
            updates_on = bool(self._org.pluginSetting(self.name(), "check_updates"))
            if not wants and not (startup and updates_on):
                return
            box = {}

            def work():
                try:
                    box["m"] = self._load_manifest()
                except Exception as e:
                    box["e"] = e

            t = threading.Thread(target=work, daemon=True)
            t.start()
            timer = QTimer(self._parent)

            def poll():
                if t.is_alive():
                    return
                timer.stop()
                timer.deleteLater()
                if "e" in box:
                    if wants:  # stay quiet about a failed update check, but not about picked addons
                        QMessageBox.warning(self._parent, "LEWD Addons", "Your mods asked for addons, but the "
                                            "addon list could not be loaded:\n%s" % box["e"])
                    return
                self._offer(box["m"], wants, updates_on)

            timer.timeout.connect(poll)
            timer.start(250)

        def _offer(self, manifest, wants, updates_on):
            installed = scan_mods(self._mods_dir(), manifest, self._active())
            by_id = {a["id"]: a for a in manifest.get("addons", []) if a.get("url")}
            dismissed = set(json.loads(self._org.persistent(self.name(), "dismissed", "[]") or "[]"))
            offers = []
            for aid in wants:
                if aid not in installed and aid in by_id:
                    offers.append("%s@%s" % (aid, by_id[aid]["version"]))
            if updates_on:
                for aid, have in installed.items():
                    a = by_id.get(aid)
                    if a and vtuple(a["version"]) > vtuple(have["version"]):
                        offers.append("%s@%s" % (aid, a["version"]))
            offers = [o for o in offers if o not in dismissed and o.split("@")[0] not in dismissed]
            if not offers:
                return
            ids = [o.split("@")[0] for o in offers]
            dlg = AddonDialog(self._parent, manifest, installed, preselect=ids,
                              intro="New downloads for your LEWD mods: the ones you picked in an installer and "
                                    "newer versions of what you have (e.g. new scopes). Close = don't ask again "
                                    "for these versions; Tools -> LEWD Addons always shows everything.")
            if dlg.exec():
                self._install(dlg.picked(), manifest, installed)
            else:
                self._org.setPersistent(self.name(), "dismissed", json.dumps(sorted(dismissed | set(offers))))

        # --- main entry (Tools menu)
        def display(self):
            try:
                manifest = self._load_manifest()
            except Exception as e:
                QMessageBox.warning(self._parent, "LEWD Addons", "Could not load the addon list:\n%s" % e)
                return
            self._offer_plugin_update(manifest)
            installed = scan_mods(self._mods_dir(), manifest, self._active())
            dlg = AddonDialog(self._parent, manifest, installed, preselect=wanted_markers(self._mods_dir()).keys(),
                              exes_cb=self._exes_panel)
            if dlg.exec():
                self._install(dlg.picked(), manifest, installed)

        def _offer_plugin_update(self, manifest):
            p = manifest.get("plugin") or {}
            if not p.get("url") or vtuple(p.get("version")) <= vtuple(__version__):
                return
            if QMessageBox.question(self._parent, "LEWD Addons",
                                    "LEWD Addon Manager %s is available (you have %s). Update now?"
                                    % (p["version"], __version__)) != QMessageBox.StandardButton.Yes:
                return
            try:
                me = Path(__file__)
                tmp = download(p["url"], me.with_suffix(".new"), p.get("sha256"))
                os.replace(tmp, me)
                QMessageBox.information(self._parent, "LEWD Addons", "Updated. Restart MO2 to use the new version.")
            except Exception as e:
                QMessageBox.warning(self._parent, "LEWD Addons", "Update failed:\n%s" % e)

        def _install(self, picked, manifest, installed):
            todo, problems = resolve(picked, manifest, installed)
            if problems:
                QMessageBox.warning(self._parent, "LEWD Addons", "\n".join(problems))
            if not todo:
                return
            total = sum(a.get("size", 0) for a in todo)
            if QMessageBox.question(self._parent, "LEWD Addons",
                                    "Download and install %d addon(s), %s?\n\n%s"
                                    % (len(todo), human(total), "\n".join(a.get("name", a["id"]) for a in todo))
                                    ) != QMessageBox.StandardButton.Yes:
                return
            dl_dir = Path(self._org.downloadsPath()) / "LEWD addons"
            state = {"i": 0, "done": 0, "total": 0, "error": None, "files": [], "finished": False}
            cancel = threading.Event()

            def work():
                try:
                    for i, a in enumerate(todo):
                        state.update(i=i, done=0, total=a.get("size", 0))
                        name = a.get("file") or "%s-%s.zip" % (a["id"], a["version"])
                        f = download(a["url"], dl_dir / name, a.get("sha256"),
                                     progress=lambda d, t: state.update(done=d, total=t or state["total"]),
                                     cancel=cancel)
                        state["files"].append((a, f))
                except Exception as e:
                    state["error"] = e
                state["finished"] = True

            pd = QProgressDialog("Downloading...", "Cancel", 0, 1000, self._parent)
            pd.setWindowTitle("LEWD Addons")
            pd.setWindowModality(Qt.WindowModality.WindowModal)
            pd.canceled.connect(cancel.set)
            threading.Thread(target=work, daemon=True).start()
            timer = QTimer(pd)

            def tick():
                a = todo[min(state["i"], len(todo) - 1)]
                pd.setLabelText("%s (%d/%d)\n%s / %s" % (a.get("name", a["id"]), state["i"] + 1, len(todo),
                                                          human(state["done"]), human(state["total"])))
                pd.setValue(int(1000 * state["done"] / state["total"]) if state["total"] else 0)
                if state["finished"]:
                    timer.stop()
                    pd.close()
                    self._after_download(state)

            timer.timeout.connect(tick)
            timer.start(200)
            pd.show()

        def _after_download(self, state):
            if state["error"] and not isinstance(state["error"], InterruptedError):
                QMessageBox.warning(self._parent, "LEWD Addons", "Download failed:\n%s" % state["error"])
            done = []
            for a, f in state["files"]:
                try:
                    if zip_has_fomod(f):
                        mod = self._org.installMod(str(f), a["mod_name"])
                        if mod is not None:
                            done.append((a, mod.name()))
                    else:
                        extract_mod(f, self._mods_dir(), a, bool(self._org.pluginSetting(self.name(), "hd_icons")))
                        done.append((a, a["mod_name"]))
                except Exception as e:
                    QMessageBox.warning(self._parent, "LEWD Addons", "Installing %s failed:\n%s" % (a["mod_name"], e))
            if not done:
                return
            self._org.refresh(True)
            QTimer.singleShot(1500, lambda: self._place(done, 0))

        def _place(self, done, attempt):
            """Enable each new mod and put it right below (= after, wins over) the mod it belongs to."""
            ml = self._org.modList()
            names = set(ml.allMods())
            if any(n not in names for _, n in done) and attempt < 10:
                QTimer.singleShot(1000, lambda: self._place(done, attempt + 1))
                return
            installed = scan_mods(self._mods_dir(), self._manifest or {}, self._active())
            woke = []
            for a, n in done:
                if n not in names:
                    continue
                base = installed.get(a.get("after", ""), {})
                after = base.get("mod")
                if after and after in names:
                    if not base.get("active") and after not in woke:
                        ml.setActive(after, True)  # MO2 leaves a fresh FOMOD install unticked
                        woke.append(after)
                    ml.setPriority(n, ml.priority(after) + 1)
                ml.setActive(n, True)
            # rebuild MO2's file tree: without it the new mods have no "origin" yet and the next drag/toggle of
            # them raises "invalid origin name" (seen 2026-10-13)
            self._org.refresh(True)
            QMessageBox.information(self._parent, "LEWD Addons",
                                    "Installed and enabled:\n" + "\n".join(n for _, n in done) +
                                    ("\n\nAlso ticked (it was installed but not enabled):\n" + "\n".join(woke)
                                     if woke else ""))


        # --- LEWD Modded Exes
        def _game_dir(self):
            return Path(self._org.managedGame().gameDirectory().absolutePath())

        def _exes_url(self):
            return self._org.pluginSetting(self.name(), "exes_manifest_url") or EXES_MANIFEST

        def _on_about_to_run(self, path, *rest):
            """MO2 is about to start bin/X.exe: start bin_lewd/X.exe instead when it exists and the setting is on."""
            try:
                if not self._org.pluginSetting(self.name(), "use_lewd_exes"):
                    return True
                target = exes_redirect(self._game_dir(), path)
                if target is None:
                    return True
                args = rest[1] if len(rest) >= 2 and isinstance(rest[1], str) else ""
                self._org.startApplication(str(target), [args] if args else [], str(target.parent))
                return False
            except Exception:
                return True  # any problem: let the stock exe run

        def _check_exes(self, manual):
            """Load lewd-exes.json in the background, then offer an install/update (never a downgrade)."""
            if not manual and not self._org.pluginSetting(self.name(), "exes_updates"):
                return
            box = {}

            def work():
                try:
                    box["m"] = fetch_exes_manifest(self._exes_url())
                except Exception as e:
                    box["e"] = e

            t = threading.Thread(target=work, daemon=True)
            t.start()
            timer = QTimer(self._parent)

            def poll():
                if t.is_alive():
                    return
                timer.stop()
                timer.deleteLater()
                if "e" in box:
                    if manual:
                        QMessageBox.warning(self._parent, "LEWD Modded Exes", "Could not load the release info:\n%s" % box["e"])
                    return
                self._offer_exes(box["m"], manual)

            timer.timeout.connect(poll)
            timer.start(250)

        def _offer_exes(self, manifest, manual):
            have = exes_installed(self._game_dir())
            new = manifest["version"]
            if have and vtuple(new) <= vtuple(have):
                if manual:
                    QMessageBox.information(self._parent, "LEWD Modded Exes", "You have the newest build (%s)." % have)
                return
            # Updates are asked once per version; the first install only once ever (or when a mod asks for it
            # with the FOMOD marker lewd_addons/want/lewd.exes). The LEWD Addons window can always install.
            wanted = "lewd.exes" in wanted_markers(self._mods_dir())
            key = ("exes@" + new) if have else ("exes@first" if not wanted else "exes@want@" + new)
            dismissed = set(json.loads(self._org.persistent(self.name(), "dismissed", "[]") or "[]"))
            if not manual and key in dismissed:
                return
            if have:
                text = "LEWD Modded Exes %s is out (you have %s). Update now?" % (new, have)
            else:
                text = ("Install the LEWD Modded Exes %s?\n\nThe LEWD engine build some LEWD mods need. They go into "
                        "a separate bin_lewd folder; your bin folder stays as it is. MO2 then starts them whenever "
                        "you start the game, and you can switch back any time in Tools > LEWD Addons." % new)
            if QMessageBox.question(self._parent, "LEWD Modded Exes", text) != QMessageBox.StandardButton.Yes:
                if not manual:
                    self._org.setPersistent(self.name(), "dismissed", json.dumps(sorted(dismissed | {key})))
                return
            self._install_exes(manifest)

        def _install_exes(self, manifest):
            url = exes_zip_url(self._exes_url(), manifest)
            dl_dir = Path(self._org.downloadsPath()) / "LEWD addons"
            game, mods = self._game_dir(), self._mods_dir()
            state = {"done": 0, "total": 0, "error": None, "version": None, "finished": False}
            cancel = threading.Event()

            def work():
                try:
                    f = download(url, dl_dir / manifest["zip"], None,
                                 progress=lambda d, t: state.update(done=d, total=t), cancel=cancel)
                    pkg = exes_extract(f, manifest, dl_dir / "lewd_exes_unpacked")
                    state["version"] = exes_install(game, mods, pkg, manifest)
                    shutil.rmtree(pkg, ignore_errors=True)
                except Exception as e:
                    state["error"] = e
                state["finished"] = True

            pd = QProgressDialog("Downloading LEWD Modded Exes...", "Cancel", 0, 1000, self._parent)
            pd.setWindowTitle("LEWD Modded Exes")
            pd.setWindowModality(Qt.WindowModality.WindowModal)
            pd.canceled.connect(cancel.set)
            threading.Thread(target=work, daemon=True).start()
            timer = QTimer(pd)

            def tick():
                pd.setLabelText("LEWD Modded Exes %s\n%s / %s" % (manifest["version"], human(state["done"]),
                                                                  human(state["total"])))
                pd.setValue(int(1000 * state["done"] / state["total"]) if state["total"] else 0)
                if state["finished"]:
                    timer.stop()
                    pd.close()
                    if state["error"]:
                        if not isinstance(state["error"], InterruptedError):
                            QMessageBox.warning(self._parent, "LEWD Modded Exes",
                                                "Install failed, your game was not changed:\n%s" % state["error"])
                        return
                    self._org.refresh(True)
                    QTimer.singleShot(1500, lambda: self._place_exes_mod(state["version"], 0))

            timer.timeout.connect(tick)
            timer.start(200)
            pd.show()

        def _place_exes_mod(self, version, attempt):
            ml = self._org.modList()
            if EXES_MOD not in set(ml.allMods()) and attempt < 10:
                QTimer.singleShot(1000, lambda: self._place_exes_mod(version, attempt + 1))
                return
            if EXES_MOD in set(ml.allMods()):
                ml.setActive(EXES_MOD, True)
                ml.setPriority(EXES_MOD, 0)  # lowest: every other mod wins over its files
                self._org.refresh(True)
            QMessageBox.information(self._parent, "LEWD Modded Exes",
                                    "LEWD Modded Exes %s installed.\n\nStart the game from MO2 as usual. On the first "
                                    "install, clear the shader cache once in the Anomaly launcher." % version)

        def _exes_panel(self):
            game = self._game_dir()
            have = exes_installed(game) or "not installed"
            on = bool(self._org.pluginSetting(self.name(), "use_lewd_exes"))
            box = QMessageBox(self._parent)
            box.setWindowTitle("LEWD Modded Exes")
            box.setText("Installed: %s\nStarting: %s exes" % (have, "LEWD (bin_lewd)" if on else "stock (bin)"))
            b_check = box.addButton("Install / update", QMessageBox.ButtonRole.AcceptRole)
            b_toggle = box.addButton("Use stock exes" if on else "Use LEWD exes", QMessageBox.ButtonRole.ActionRole)
            b_back = box.addButton("Roll back", QMessageBox.ButtonRole.ActionRole)
            b_back.setEnabled((game / (EXES_BIN + ".prev")).is_dir())
            box.addButton(QMessageBox.StandardButton.Close)
            box.exec()
            clicked = box.clickedButton()
            if clicked == b_check:
                self._check_exes(True)
            elif clicked == b_toggle:
                self._org.setPluginSetting(self.name(), "use_lewd_exes", not on)
            elif clicked == b_back:
                try:
                    v = exes_rollback(game)
                    QMessageBox.information(self._parent, "LEWD Modded Exes", "Rolled back to %s." % (v or "the previous build"))
                except Exception as e:
                    QMessageBox.warning(self._parent, "LEWD Modded Exes", str(e))


def createPlugin():
    return LewdAddonManager()
