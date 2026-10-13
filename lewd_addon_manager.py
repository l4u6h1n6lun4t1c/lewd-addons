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

__version__ = "0.1.1"
DEFAULT_MANIFEST = "https://github.com/l4u6h1n6lun4t1c/lewd-addons/releases/latest/download/index.json"
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


def scan_mods(mods_dir, manifest):
    """-> {addon id: {"mod": folder name, "version": str}} for every installed LEWD mod we can identify.
    Several folders with the same id: the highest version wins."""
    found = {}
    detect = [(a["id"], a["detect"]) for a in manifest.get("addons", []) if a.get("detect")]
    for d in sorted(Path(mods_dir).iterdir()) if Path(mods_dir).is_dir() else []:
        if not d.is_dir() or d.name.endswith("_separator"):
            continue
        hits = []
        idf = d / ID_FILE
        if idf.is_file():
            try:
                info = json.loads(idf.read_text("utf-8"))
                hits.append((info["id"], str(info.get("version", "0"))))
            except Exception:
                pass
        for aid, rule in detect:
            f = rule.get("file")
            if f and (d / f).is_file() and aid not in [h[0] for h in hits]:
                hits.append((aid, read_meta_version(d) or "0"))
        for aid, ver in hits:
            cur = found.get(aid)
            if cur is None or vtuple(ver) > vtuple(cur["version"]):
                found[aid] = {"mod": d.name, "version": ver}
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
            problems.append("%s needs '%s' %s, which is not in the addon list." % (why, aid, minv))
            return False
        name = a.get("name", aid)
        if vtuple(a.get("version")) < vtuple(minv):
            problems.append("%s needs %s %s, newest available is %s." % (why, name, minv, a.get("version")))
            return False
        if not a.get("url"):
            where = (" Get it here: " + a["page"]) if a.get("page") else ""
            problems.append("%s needs %s %s, install it by hand.%s" % (why, name, minv, where))
            return False
        if not all([need(r["id"], r.get("min", "0"), name) for r in a.get("requires", [])]):
            problems.append("%s skipped." % name)
            return False
        order.append(a)
        ok[aid] = True
        return True

    for aid in picked:
        visit(aid, "You")
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


def extract_mod(zip_path, mods_dir, addon):
    """Plain addon zip (gamedata/... + lewd_addon.json) -> <mods>/<mod_name>, replacing an older copy."""
    target = Path(mods_dir) / addon["mod_name"]
    tmp = target.with_name(target.name + ".lewdtmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    with zipfile.ZipFile(zip_path) as z:
        for n in z.namelist():
            p = Path(n)
            if p.is_absolute() or ".." in p.parts:
                raise ValueError("unsafe path in zip: %s" % n)
        z.extractall(tmp)
    (tmp / ID_FILE).write_text(json.dumps({"id": addon["id"], "version": addon["version"]}, indent=1), "utf-8")
    (tmp / "meta.ini").write_text(
        "[General]\ngameName=stalkeranomaly\nmodid=0\nversion=%s\ninstallationFile=%s\nurl=%s\n"
        "comments=installed by LEWD Addon Manager\n" % (addon["version"], Path(zip_path).name, addon.get("url", "")),
        "utf-8")
    if target.exists():
        shutil.rmtree(target)
    os.replace(tmp, target)
    return target


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%.0f %s" if unit in ("B", "KB") else "%.1f %s") % (n, unit)
        n /= 1024.0


# ---------------------------------------------------------------- MO2 side

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
        def __init__(self, parent, manifest, installed, preselect=()):
            super().__init__(parent)
            self.setWindowTitle("LEWD Addons")
            self.resize(820, 520)
            lay = QVBoxLayout(self)
            lay.addWidget(QLabel("Tick the addons to install or update. Requirements are added automatically."))
            self.tree = QTreeWidget()
            self.tree.setHeaderLabels(["Addon", "Size", "Installed", "Available", "Notes"])
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
            bb.addButton("Install / update ticked", QDialogButtonBox.ButtonRole.AcceptRole)
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
            organizer.onUserInterfaceInitialized(lambda win: QTimer.singleShot(3000, self._check_markers))
            ml = organizer.modList()
            if hasattr(ml, "onModInstalled"):
                ml.onModInstalled(lambda mod: QTimer.singleShot(1500, self._check_markers))
            return True

        def name(self):
            return "LEWD Addon Manager"

        def author(self):
            return "LEWD"

        def description(self):
            return "Downloads and installs optional parts of LEWD mods (camos, scopes, textures, attachments)."

        def version(self):
            p = [int(x) for x in __version__.split(".")]
            return mobase.VersionInfo(p[0], p[1], p[2], mobase.ReleaseType.FINAL)

        def settings(self):
            return [mobase.PluginSetting("manifest_url", "Addon list URL (https:// or file:///)", DEFAULT_MANIFEST),
                    mobase.PluginSetting("ask_on_start", "Offer addons picked in a FOMOD", True)]

        def displayName(self):
            return "LEWD Addons"

        def tooltip(self):
            return "Download optional parts of LEWD mods"

        def icon(self):
            return QIcon()

        def setParentWidget(self, widget):
            self._parent = widget

        # --- helpers
        def _mods_dir(self):
            return Path(self._org.modsPath())

        def _load_manifest(self):
            url = self._org.pluginSetting(self.name(), "manifest_url") or DEFAULT_MANIFEST
            override = Path(__file__).with_suffix(".url")  # dev/test: lewd_addon_manager.url next to the plugin
            if url == DEFAULT_MANIFEST and override.is_file():
                url = override.read_text("utf-8").strip() or url
            self._manifest = fetch_manifest(url)
            return self._manifest

        def _check_markers(self):
            if not self._org.pluginSetting(self.name(), "ask_on_start"):
                return
            wants = wanted_markers(self._mods_dir())
            if not wants:
                return
            try:
                manifest = self._load_manifest()
            except Exception as e:
                QMessageBox.warning(self._parent, "LEWD Addons",
                                    "Your mods asked for addons, but the addon list could not be loaded:\n%s" % e)
                return
            installed = scan_mods(self._mods_dir(), manifest)
            ask = [aid for aid in wants if aid not in installed]
            dismissed = set(json.loads(self._org.persistent(self.name(), "dismissed", "[]") or "[]"))
            ask = [a for a in ask if a not in dismissed]
            if not ask:
                return
            dlg = AddonDialog(self._parent, manifest, installed, preselect=ask)
            if dlg.exec():
                self._install(dlg.picked(), manifest, installed)
            else:
                self._org.setPersistent(self.name(), "dismissed", json.dumps(sorted(dismissed | set(ask))))

        # --- main entry (Tools menu)
        def display(self):
            try:
                manifest = self._load_manifest()
            except Exception as e:
                QMessageBox.warning(self._parent, "LEWD Addons", "Could not load the addon list:\n%s" % e)
                return
            self._offer_plugin_update(manifest)
            installed = scan_mods(self._mods_dir(), manifest)
            dlg = AddonDialog(self._parent, manifest, installed, preselect=wanted_markers(self._mods_dir()).keys())
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
                        extract_mod(f, self._mods_dir(), a)
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
            installed = scan_mods(self._mods_dir(), self._manifest or {})
            for a, n in done:
                if n not in names:
                    continue
                after = installed.get(a.get("after", ""), {}).get("mod")
                if after and after in names:
                    ml.setPriority(n, ml.priority(after) + 1)
                ml.setActive(n, True)
            QMessageBox.information(self._parent, "LEWD Addons",
                                    "Installed and enabled:\n" + "\n".join(n for _, n in done))


def createPlugin():
    return LewdAddonManager()
