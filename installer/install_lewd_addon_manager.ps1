# Installs lewd_addon_manager.py (next to this script) into Mod Organizer 2's plugins folder.
# Finds MO2 by: the folder this script sits in (and its parents), a running ModOrganizer.exe, then asks with a folder picker.
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$plugin = Join-Path $here 'lewd_addon_manager.py'
if (-not (Test-Path $plugin)) { Write-Host "lewd_addon_manager.py is missing next to this installer. Unpack the whole zip first."; exit 1 }

function Test-Mo2($dir) { $dir -and (Test-Path (Join-Path $dir 'ModOrganizer.exe')) }

$mo2 = $null
$d = $here
while ($d -and -not $mo2) { if (Test-Mo2 $d) { $mo2 = $d } else { $d = Split-Path -Parent $d } }
if (-not $mo2) {
  $p = Get-Process ModOrganizer -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($p -and $p.Path) { $mo2 = Split-Path -Parent $p.Path }
}
if (-not $mo2) {
  Add-Type -AssemblyName System.Windows.Forms
  $dlg = New-Object System.Windows.Forms.FolderBrowserDialog
  $dlg.Description = 'Pick your Mod Organizer folder (the one with ModOrganizer.exe, e.g. your GAMMA folder)'
  while ($true) {
    if ($dlg.ShowDialog() -ne 'OK') { Write-Host 'Cancelled.'; exit 1 }
    if (Test-Mo2 $dlg.SelectedPath) { $mo2 = $dlg.SelectedPath; break }
    [System.Windows.Forms.MessageBox]::Show('No ModOrganizer.exe in that folder. Pick the folder that contains it.') | Out-Null
  }
}
Write-Host "Mod Organizer: $mo2"

# MO2 loads plugins at start; it must not be running while we copy (and it has to restart anyway)
while (Get-Process ModOrganizer -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($mo2, 'OrdinalIgnoreCase') }) {
  Read-Host 'Close Mod Organizer, then press Enter' | Out-Null
}

$dest = Join-Path $mo2 'plugins'
if (-not (Test-Path $dest)) { New-Item -ItemType Directory $dest | Out-Null }
$target = Join-Path $dest 'lewd_addon_manager.py'
function Get-PluginVersion($f) {
  $m = Select-String -Path $f -Pattern '^__version__ = "([^"]+)"' -List
  if ($m) { [version]($m.Matches[0].Groups[1].Value) } else { [version]'0.0' }
}
$new = Get-PluginVersion $plugin
if (Test-Path $target) {
  $old = Get-PluginVersion $target
  if ($old -gt $new) { Write-Host "You already have a newer LEWD Addon Manager ($old). Nothing to do."; exit 0 }
}
Copy-Item $plugin $target -Force
Write-Host ""
Write-Host "LEWD Addon Manager $new installed into $dest"
Write-Host "Start Mod Organizer: Tools > LEWD Addons. It updates itself from now on."
exit 0
