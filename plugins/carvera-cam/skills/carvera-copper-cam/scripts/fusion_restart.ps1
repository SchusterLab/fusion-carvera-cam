<#
Checks on Fusion, and starts it again after a crash or a hang (Windows only).

With -Status, it only reports Fusion's state: whether it runs, whether its window
responds, its CPU seconds and memory, and whether a "Fusion Error Report" window
(cer_dialog.exe) is open. Two reports a few seconds apart tell a busy Fusion (its
CPU seconds climb) from a stopped one; a window that does not respond only means
that a script or a toolpath is still running.

Otherwise it
  1. with -Stop, stops a Fusion that is still running (a hang: unsaved documents
     are lost apart from their crash recovery);
  2. closes the Fusion Error Report window, if any;
  3. with Fusion stopped, clears the crash-recovery entries of the documents whose
     creationId is in -Throwaway, each one's element in AutoSavedFileList.xml and its
     .backup file, so that the Recovered Documents window, which blocks every MCP
     call, does not open for them; it lists every other entry under "kept", and
     leaves those for the user to decide on;
  4. starts Fusion from its Start-menu shortcut (a FusionLauncher.exe from an older
     webdeploy folder fails with "Configuration file ... is missing or incomplete");
  5. waits until Fusion's window responds and its title, past "Initializing" and
     "Loading additional modules", stays the same for 5 s;
and prints the result as JSON. It exits with 1 if Fusion has not started within
-TimeoutS seconds, or if it is still running and -Stop was not given.

  powershell -ExecutionPolicy Bypass -File fusion_restart.ps1 -Status
  powershell -ExecutionPolicy Bypass -File fusion_restart.ps1 [-Stop] [-Throwaway <id>,<id>] [-TimeoutS 300]
#>
param([switch]$Status, [switch]$Stop, [string[]]$Throwaway = @(), [int]$TimeoutS = 300)
$ErrorActionPreference = 'Stop'
$t0 = Get-Date

function State {
    $p = Get-Process Fusion360 -ErrorAction SilentlyContinue | Select-Object -First 1
    $d = Get-Process cer_dialog -ErrorAction SilentlyContinue
    if ($p) {
        [ordered]@{ running = $true; pid = $p.Id; responding = $p.Responding;
            title = $p.MainWindowTitle; cpu_s = [math]::Round($p.CPU, 1);
            memory_mb = [int]($p.WorkingSet64 / 1MB); error_dialog = [bool]$d }
    } else {
        [ordered]@{ running = $false; error_dialog = [bool]$d }
    }
}

if ($Status) {
    State | ConvertTo-Json
    exit 0
}

$out = [ordered]@{ stopped = $false; dialog_closed = $false; cleared = @(); kept = @() }
$p = Get-Process Fusion360 -ErrorAction SilentlyContinue
if ($p) {
    if (-not $Stop) {
        $out.error = 'Fusion is still running: pass -Stop to stop it'
        $out.state = State
        $out | ConvertTo-Json -Depth 4
        exit 1
    }
    $p | Stop-Process -Force
    $p | Wait-Process -Timeout 60 -ErrorAction SilentlyContinue
    $out.stopped = $true
}
$d = Get-Process cer_dialog -ErrorAction SilentlyContinue
if ($d) {
    $d | Stop-Process -Force
    $out.dialog_closed = $true
}

# the crash-recovery list: one element per document, with its Name, its DocGuid
# (the document's creationId) and its RecoveryPath, a .backup file, read either
# as attributes or as child elements
$root = Join-Path $env:LOCALAPPDATA 'Autodesk\Autodesk Fusion 360'
foreach ($f in Get-ChildItem $root -Directory -ErrorAction SilentlyContinue) {
    $list = Join-Path $f.FullName 'CrashRecovery\AutoSavedFileList.xml'
    if (-not (Test-Path $list)) { continue }
    $xml = New-Object System.Xml.XmlDocument
    $xml.PreserveWhitespace = $true
    $xml.Load($list)
    $changed = $false
    foreach ($e in @($xml.DocumentElement.ChildNodes | Where-Object { $_.NodeType -eq 'Element' })) {
        $get = { param($n) if ($e.HasAttribute($n)) { $e.GetAttribute($n) } elseif ($e[$n]) { $e[$n].InnerText } else { '' } }
        $entry = [ordered]@{ name = (& $get 'Name'); creationId = (& $get 'DocGuid') }
        $backup = & $get 'RecoveryPath'
        if ($Throwaway -contains $entry.creationId) {
            if ($backup -and (Test-Path $backup)) { Remove-Item $backup -Force }
            [void]$e.ParentNode.RemoveChild($e)
            $changed = $true
            $out.cleared += $entry
        } else {
            $out.kept += $entry
        }
    }
    if ($changed) { $xml.Save($list) }
}

$lnk = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Autodesk\Autodesk Fusion.lnk'
Start-Process $lnk
$ready = $false
$last = ''
while (((Get-Date) - $t0).TotalSeconds -lt $TimeoutS) {
    Start-Sleep 5
    $p = Get-Process Fusion360 -ErrorAction SilentlyContinue | Select-Object -First 1
    $title = if ($p -and $p.Responding) { $p.MainWindowTitle } else { '' }
    # starting up, the title goes "Initializing...", "Loading additional modules";
    # ready, it names the open document, and stays so
    if ($title -and $title -notmatch '^(Initializing|Loading)' -and $title -eq $last) {
        $ready = $true
        break
    }
    $last = $title
}
$out.started = $ready
$out.state = State
$out.seconds = [int]((Get-Date) - $t0).TotalSeconds
$out | ConvertTo-Json -Depth 4
if (-not $ready) { exit 1 }
