<#
winhands installer for Windows 10 2004+/11. No admin rights needed.

  Install / update:
    irm https://raw.githubusercontent.com/zredo19/winhands/main/install.ps1 | iex

  Uninstall (iex cannot take arguments, so build a script block):
    & ([scriptblock]::Create((irm https://raw.githubusercontent.com/zredo19/winhands/main/install.ps1))) -Uninstall
  or download this file and run:  .\install.ps1 -Uninstall

  It installs uv if missing, installs winhands as a uv tool and runs `python -m winhands setup` with the tool's
  python (registers the MCP server in Claude Code and installs the skill). It never runs winhands.exe: Windows
  App Control / Smart App Control can block that unsigned launcher.
  -DryRun prints what would run without changing anything.
  WINHANDS_SPEC overrides the package source (testing).
#>
param([switch]$Uninstall, [switch]$DryRun)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

# Package source: PyPI. Override with WINHANDS_SPEC (e.g. git+https://github.com/zredo19/winhands to try unreleased code).
$Spec = if ($env:WINHANDS_SPEC) { $env:WINHANDS_SPEC } else { 'winhands' }

function Say($msg) { Write-Host "[winhands] $msg" -ForegroundColor Cyan }

# Runs $sb unless -DryRun; any failure becomes one clear message naming the step.
function Step($what, [scriptblock]$sb) {
    if ($DryRun) { Say "(dry run) $what"; return }
    Say $what
    try { & $sb } catch { throw "Failed to $what - $($_.Exception.Message)" }
}

function Add-Path($dir) {
    if ($dir -and (Test-Path $dir) -and (($env:Path -split ';') -notcontains $dir)) { $env:Path = "$dir;$env:Path" }
}

function Get-Uv {
    Add-Path (Join-Path $HOME '.local\bin')  # where the uv installer puts uv.exe
    Get-Command uv -ErrorAction SilentlyContinue
}

# python.exe of the winhands tool venv: winhands runs as `python -m winhands`, never through winhands.exe.
# The placeholders are only reachable in -DryRun before uv exists.
function Get-ToolPython {
    $dir = if (Get-Uv) { (uv tool dir | Out-String).Trim() } else { 'UV_TOOL_DIR' }
    "$dir\winhands\Scripts\python.exe"
}

function Get-ToolBin {
    if (Get-Uv) { (uv tool dir --bin | Out-String).Trim() } else { 'UV_TOOL_BIN_DIR' }
}

function Install-Winhands {
    if (-not (Get-Uv)) {
        Step 'install uv (Python package manager)' { Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression }
        if (-not $DryRun -and -not (Get-Uv)) { throw 'uv was installed but is not on PATH yet. Open a new terminal and run this again.' }
    }

    # uv downloads a managed Python >= 3.11 by itself, so no Python is needed here.
    # --compile-bytecode: without it the first MCP start took ~18 s (Claude Code may time out), with it ~3 s.
    Step "install winhands from $Spec" {
        uv tool install --force --compile-bytecode $Spec
        if ($LASTEXITCODE) { throw "uv exited with code $LASTEXITCODE" }
    }

    $py = Get-ToolPython
    $bin = Get-ToolBin
    # Persistent PATH only matters for the `winhands` command in new shells; Claude Code does not need it, so a failure here is not fatal.
    $newTerminal = $false
    if (-not $DryRun -and (([Environment]::GetEnvironmentVariable('Path', 'User') -split ';') -notcontains $bin)) {
        try {
            Say 'add the uv tool folder to your PATH'
            uv tool update-shell
            if ($LASTEXITCODE) { throw "uv exited with code $LASTEXITCODE" }
            $newTerminal = $true
        }
        catch { Say "Could not add the uv tool folder to your PATH ($($_.Exception.Message)); Claude Code does not need it." }
    }
    Add-Path $bin

    $manual = "& `"$py`" -m winhands setup"
    try {
        Step 'register winhands with Claude Code and install the skill (python -m winhands setup)' {
            if (-not (Test-Path $py)) { throw "the tool python was not found at $py" }
            & $py -m winhands setup
            if ($LASTEXITCODE) { throw "exit code $LASTEXITCODE" }
        }
    }
    catch { throw "$($_.Exception.Message)`nwinhands itself is installed. To finish by hand, run:  $manual" }

    if ($DryRun) { Say 'Dry run finished. Nothing was changed.'; return }
    Say 'Done. Restart Claude Code, then ask it: "open Notepad and type hello".'
    if ($newTerminal) { Say 'Open a new terminal if you want to run the `winhands` command yourself (PATH changed).' }
}

function Uninstall-Winhands {
    if (-not (Get-Uv)) { Say 'uv is not installed, so winhands was not installed as a uv tool.'; return }
    $py = Get-ToolPython
    if ($DryRun -or (Test-Path $py)) {
        Step 'remove the MCP entry and the skill (python -m winhands setup --remove)' { & $py -m winhands setup --remove }
    }
    Step 'uninstall winhands' { uv tool uninstall winhands; if ($LASTEXITCODE) { Say 'winhands was not installed as a uv tool.' } }
    if ($DryRun) { Say 'Dry run finished. Nothing was changed.'; return }
    Say 'Removed. uv itself was left installed. Your winhands notes (if any) were not touched.'
}

try {
    if ($Uninstall) { Uninstall-Winhands } else { Install-Winhands }
}
catch {
    Write-Host "[winhands] $($_.Exception.Message)" -ForegroundColor Red
    $global:LASTEXITCODE = 1
}
