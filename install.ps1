<#
winhands installer for Windows 10 2004+/11. No admin rights needed.

  Install / update:
    irm https://raw.githubusercontent.com/zredo19/winhands/main/install.ps1 | iex

  Uninstall (iex cannot take arguments, so build a script block):
    & ([scriptblock]::Create((irm https://raw.githubusercontent.com/zredo19/winhands/main/install.ps1))) -Uninstall
  or download this file and run:  .\install.ps1 -Uninstall

  It installs uv if missing, installs winhands as a uv tool and runs `winhands setup`
  (registers the MCP server in Claude Code and installs the skill).
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

# Full path of the installed executable. The placeholder is only reachable in -DryRun before uv exists.
function Get-Exe {
    $bin = if (Get-Uv) { (uv tool dir --bin | Out-String).Trim() } else { 'UV_TOOL_BIN_DIR' }
    "$bin\winhands.exe"
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

    $exe = Get-Exe
    $bin = Split-Path $exe
    # Persistent PATH only matters for the `winhands` command in new shells; setup registers the full path.
    $newTerminal = $false
    if (-not $DryRun -and (([Environment]::GetEnvironmentVariable('Path', 'User') -split ';') -notcontains $bin)) {
        Step 'add the uv tool folder to your PATH' { uv tool update-shell; if ($LASTEXITCODE) { throw "uv exited with code $LASTEXITCODE" } }
        $newTerminal = $true
    }
    Add-Path $bin

    Step 'register winhands with Claude Code and install the skill (winhands setup)' {
        & $exe setup
        if ($LASTEXITCODE) { throw "winhands setup exited with code $LASTEXITCODE" }
    }

    if ($DryRun) { Say 'Dry run finished. Nothing was changed.'; return }
    Say 'Done. Restart Claude Code, then ask it: "open Notepad and type hello".'
    if ($newTerminal) { Say 'Open a new terminal if you want to run the `winhands` command yourself (PATH changed).' }
}

function Uninstall-Winhands {
    if (-not (Get-Uv)) { Say 'uv is not installed, so winhands was not installed as a uv tool.'; return }
    $exe = Get-Exe
    if ($DryRun -or (Test-Path $exe)) {
        Step 'remove the MCP entry and the skill (winhands setup --remove)' { & $exe setup --remove }
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
