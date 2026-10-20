<#
Drives Fusion's open simulation, which the API cannot reach (Windows only).

Run it after sim_check.py's simulate() has opened the
simulation and its Issues panel. It
  1. closes any "Info" notice Fusion opened when the simulation started (and reports its text);
  2. waits until the Issues panel's verification reaches 100%;
  3. reads every issue the panel lists, by operation;
  4. with -Stl, moves the simulation to its end (End of Toolpath, in the canvas's
     right-click menu) and saves the stock there as an STL (Save Stock..., in the same
     menu's Stock submenu), to this computer only, and moves the file to the -Stl path;
and prints the result as JSON. It leaves the simulation open: exit it with
sim_check.py's stop().

It works through window messages and UI Automation, not the real mouse or keyboard,
so it neither needs the screen nor disturbs whoever is using it.

  powershell -ExecutionPolicy Bypass -File sim_ui.ps1 [-Stl <path.stl>] [-TimeoutS 600]
#>
param([string]$Stl = '', [int]$TimeoutS = 600)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
Add-Type @'
using System; using System.Text; using System.Runtime.InteropServices; using System.Collections.Generic;
public class CcWin {
  public delegate bool P(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] static extern bool EnumWindows(P p, IntPtr l);
  [DllImport("user32.dll")] static extern bool EnumChildWindows(IntPtr par, P p, IntPtr l);
  [DllImport("user32.dll")] static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
  public struct RECT { public int L, T, R, B; }
  public class Win { public IntPtr H; public string Title; public RECT R; }
  static Win Make(IntPtr h) { var s = new StringBuilder(256); GetWindowText(h, s, 256); var w = new Win(); w.H = h; w.Title = s.ToString(); GetWindowRect(h, out w.R); return w; }
  public static List<Win> Top(uint pid) { var r = new List<Win>(); EnumWindows((h, l) => { uint p; GetWindowThreadProcessId(h, out p); if (p == pid && IsWindowVisible(h)) r.Add(Make(h)); return true; }, IntPtr.Zero); return r; }
  public static List<Win> Kids(IntPtr top) { var r = new List<Win>(); EnumChildWindows(top, (h, l) => { if (IsWindowVisible(h)) r.Add(Make(h)); return true; }, IntPtr.Zero); return r; }
}
'@
$UIA = [System.Windows.Automation.AutomationElement]
$Scope = [System.Windows.Automation.TreeScope]
function Cond($prop, $value) { New-Object System.Windows.Automation.PropertyCondition($prop, $value) }
function Fusion { $p = Get-Process Fusion360 -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1; if (-not $p) { throw 'Fusion is not running' }; $p }
function TopWindows { [CcWin]::Top((Fusion).Id) }
function Lparam([int]$x, [int]$y) { [IntPtr](($y -shl 16) -bor $x) }
function Key([IntPtr]$h, [int]$vk) {
  [CcWin]::PostMessage($h, 0x0100, [IntPtr]$vk, [IntPtr]1) | Out-Null; Start-Sleep -Milliseconds 80
  [CcWin]::PostMessage($h, 0x0101, [IntPtr]$vk, [IntPtr]0xC0000001) | Out-Null; Start-Sleep -Milliseconds 300
}
function RightClick([IntPtr]$h, [int]$x, [int]$y) {
  [CcWin]::PostMessage($h, 0x0200, [IntPtr]0, (Lparam $x $y)) | Out-Null; Start-Sleep -Milliseconds 100
  [CcWin]::PostMessage($h, 0x0204, [IntPtr]2, (Lparam $x $y)) | Out-Null; Start-Sleep -Milliseconds 120
  [CcWin]::PostMessage($h, 0x0205, [IntPtr]0, (Lparam $x $y)) | Out-Null
}
function WaitFor([scriptblock]$test, [int]$seconds, [string]$what) {
  $end = (Get-Date).AddSeconds($seconds)
  while ((Get-Date) -lt $end) { $r = & $test; if ($r) { return $r }; Start-Sleep -Milliseconds 300 }
  throw "timed out waiting for $what"
}
function Texts($el) { $el.FindAll($Scope::Descendants, [System.Windows.Automation.Condition]::TrueCondition) | ForEach-Object { $_.Current.Name } | Where-Object { $_ } }
function Press($el, [string]$name) {
  $b = $el.FindFirst($Scope::Descendants, (Cond $UIA::NameProperty $name))
  if (-not $b) { throw "no '$name' button" }
  $b.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
}

$result = [ordered]@{ notices = @(); verification = $null; errors = $null; warnings = $null; issues = @() }

# 1. Notices, such as "Multiple setups with different WCS settings have been selected".
$deadline = (Get-Date).AddSeconds(3)
while ((Get-Date) -lt $deadline) {
  foreach ($w in TopWindows | Where-Object { $_.Title -eq 'Info' }) {
    $el = $UIA::FromHandle($w.H)
    $result.notices += ((Texts $el | Where-Object { $_ -notin 'Info', 'OK' }) -join ' ')
    Press $el 'OK'
    $deadline = (Get-Date).AddSeconds(3)
  }
  Start-Sleep -Milliseconds 300
}

# 2. The Issues panel, and its verification.
$labelId = 'SimulationIssuesWidget.verificationLabel'
$panel = WaitFor {
  foreach ($w in TopWindows) {
    $el = $UIA::FromHandle($w.H)
    $all = $el.FindAll($Scope::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
    foreach ($x in $all) { if ($x.Current.AutomationId -like "*$labelId") { return @{ win = $el; label = $x } } }
  }
} 30 'the Issues panel (run sim_check.py simulate() first)'
WaitFor { $panel.label.Current.Name -match 'Verification:\s*100%' } $TimeoutS 'the simulation to finish' | Out-Null
$status = $panel.label.Current.Name
$result.verification = $status
if ($status -match 'Errors:\s*(\d+)') { $result.errors = [int]$Matches[1] }
if ($status -match 'Warnings:\s*(\d+)') { $result.warnings = [int]$Matches[1] }

# 3. Every issue, by operation: the tree's grid gives all rows, scrolled out of view or not.
$tree = $panel.win.FindFirst($Scope::Descendants, (Cond $UIA::ControlTypeProperty ([System.Windows.Automation.ControlType]::Tree)))
$grid = $tree.GetCurrentPattern([System.Windows.Automation.GridPattern]::Pattern)
$op = $null
for ($r = 0; $r -lt $grid.Current.RowCount; $r++) {
  $cells = @(for ($c = 0; $c -lt $grid.Current.ColumnCount; $c++) { $grid.GetItem($r, $c).Current.Name })
  if ($cells[0] -match '^(.*) - (Errors|Warnings): (\d+)$') {
    $op = [ordered]@{ operation = $Matches[1]; kind = $Matches[2]; count = [int]$Matches[3]; events = @() }
    $result.issues += $op
  } elseif ($op) {
    $op.events += ,@($cells | Where-Object { $_ })
  }
}

# 4. Save the stock at the simulation's end: Save Stock saves the stock at the playhead.
if ($Stl) {
  $main = (Fusion).MainWindowHandle
  # The canvas: the largest child of the main window below the toolbar.
  $canvas = [CcWin]::Kids($main) | Where-Object { $_.R.T -gt 100 } | Sort-Object { ($_.R.R - $_.R.L) * ($_.R.B - $_.R.T) } | Select-Object -Last 1
  function Menu {
    RightClick $canvas.H 60 60
    WaitFor { TopWindows | Where-Object { $_.Title -eq 'Marking Menu' } | Select-Object -First 1 } 10 'the right-click menu'
  }
  # The right-click menu: a ring of player buttons (Up and Down step round it; Up first goes
  # to End of Toolpath, and Down from there into the list below), then a list: OK, Cancel, Loop, Previous Event, Next Event, Previous Bookmark, Next Bookmark, Add Bookmark,
  # Remove Bookmark, Remove All Bookmarks, Stock; the bookmark items but Add are greyed out
  # and skipped. Stock's submenu: Load Stock..., Load Model as Stock, Save Stock...
  $m = Menu
  foreach ($k in 0x26, 0x0D) { Key $m.H $k }          # End of Toolpath
  Start-Sleep 3
  $m = Menu
  foreach ($k in @(0x26) + @(0x28) * 7 + @(0x27, 0x28, 0x28, 0x0D)) { Key $m.H $k }   # Stock > Save Stock...
  $dlg = $null
  try { $dlg = WaitFor { TopWindows | Where-Object { $_.Title -eq 'Save Stock' } | Select-Object -First 1 } 10 'the Save Stock dialog' }
  catch { $m = TopWindows | Where-Object { $_.Title -eq 'Marking Menu' }; if ($m) { Key $m[0].H 0x1B }; throw }
  $el = $UIA::FromHandle($dlg.H)
  function ById($id) { $x = $el.FindFirst($Scope::Descendants, (Cond $UIA::AutomationIdProperty $id)); if (-not $x) { throw "no $id" }; $x }
  $name = 'carvera-cam-stock-' + [Guid]::NewGuid().ToString('N').Substring(0, 8)
  (ById 'QTApplication.QTFrameWindow.MainWidget.editName').GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).SetValue($name)
  foreach ($p in @(@('QTApplication.QTFrameWindow.MainWidget.checkSaveIn', 'Off'), @('QTApplication.QTFrameWindow.standardActions.checkLocalSave', 'On'))) {
    $t = (ById $p[0]).GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern)
    if ("$($t.Current.ToggleState)" -ne $p[1]) { $t.Toggle() }
    if ("$($t.Current.ToggleState)" -ne $p[1]) { throw "could not set $($p[0]) $($p[1])" }
  }
  $folder = (ById 'QTApplication.QTFrameWindow.standardActions.labelLocalSavePath').Current.Name
  $file = Join-Path $folder "$name.stl"
  # Pressed straight after the toggles, Save sometimes does nothing; press it until the file comes.
  $end = (Get-Date).AddSeconds(60)
  while (-not ((Test-Path $file) -and (Get-Item $file).Length -gt 84)) {
    if ((Get-Date) -gt $end) { throw "timed out waiting for the STL at $file" }
    if (TopWindows | Where-Object { $_.H -eq $dlg.H }) {
      Start-Sleep 1
      try { (ById 'QTApplication.QTFrameWindow.standardActions.SaveButton').GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke() } catch {}
    }
    Start-Sleep 2
  }
  Start-Sleep 1
  # A binary STL's triangle count is at byte 80; the uncut stock box has 12.
  $bytes = [System.IO.File]::ReadAllBytes($file)
  $result.triangles = [BitConverter]::ToUInt32($bytes, 80)
  Move-Item -Force $file $Stl
  if ($result.triangles -le 12) { throw "the saved stock is the uncut stock box ($Stl): the simulation did not reach its end" }
  $result.stl = $Stl
}
$result | ConvertTo-Json -Depth 6
