<#
winhands installer for Windows 10 2004+/11. No admin rights needed.

  Install / update:
    irm https://raw.githubusercontent.com/zredo19/winhands/main/install.ps1 | iex

  Uninstall (iex cannot take arguments, so build a script block):
    & ([scriptblock]::Create((irm https://raw.githubusercontent.com/zredo19/winhands/main/install.ps1))) -Uninstall
  or download this file and run:  .\install.ps1 -Uninstall

  -DryRun prints what would run without changing anything.
  WINHANDS_SPEC / WINHANDS_RAW override the package source and the raw-file base URL (testing).
#>
param([switch]$Uninstall, [switch]$DryRun)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$Spec = if ($env:WINHANDS_SPEC) { $env:WINHANDS_SPEC } else { 'git+https://github.com/zredo19/winhands' }
$Raw = if ($env:WINHANDS_RAW) { $env:WINHANDS_RAW } else { 'https://raw.githubusercontent.com/zredo19/winhands/main' }
$SkillDir = Join-Path $HOME '.claude\skills\winhands'

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

    $bin = if (Get-Uv) { (uv tool dir --bin | Out-String).Trim() } else { 'UV_TOOL_BIN_DIR' }  # placeholder: only reachable in -DryRun before uv exists
    $exe = "$bin\winhands.exe"  # not Join-Path: it rejects the placeholder on Windows PowerShell 5.1

    # Persistent PATH only matters for the `winhands` command in new shells; the MCP entry below uses the full path.
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $newTerminal = $false
    if (-not $DryRun -and (($userPath -split ';') -notcontains $bin)) {
        Step 'add the uv tool folder to your PATH' { uv tool update-shell; if ($LASTEXITCODE) { throw "uv exited with code $LASTEXITCODE" } }
        $newTerminal = $true
    }
    Add-Path $bin

    if (Get-Command claude -ErrorAction SilentlyContinue) {
        Step 'register the MCP server in Claude Code (user scope)' {
            try { claude mcp remove winhands --scope user 2>$null | Out-Null } catch { }  # re-run = refresh the path
            claude mcp add winhands --scope user -- $exe
            if ($LASTEXITCODE) { throw "claude exited with code $LASTEXITCODE" }
        }
    }
    else {
        Say 'Claude Code CLI not found. For other MCP clients add this server to their config:'
        @{ mcpServers = @{ winhands = @{ command = $exe } } } | ConvertTo-Json -Depth 4
    }

    Step "install the Claude skill into $SkillDir" {
        New-Item -ItemType Directory -Force $SkillDir | Out-Null
        Invoke-WebRequest "$Raw/SKILL.md" -OutFile (Join-Path $SkillDir 'SKILL.md') -UseBasicParsing
    }

    if ($DryRun) { Say 'Dry run finished. Nothing was changed.'; return }
    Say 'Done. Installed: winhands (uv tool), its MCP entry, and the skill.'
    Say 'Restart Claude Code, then ask it: "open Notepad and type hello".'
    if ($newTerminal) { Say 'Open a new terminal if you want to run the `winhands` command yourself (PATH changed).' }
}

function Uninstall-Winhands {
    if (Get-Uv) {
        Step 'uninstall winhands' { uv tool uninstall winhands; if ($LASTEXITCODE) { Say 'winhands was not installed as a uv tool.' } }
    }
    if (Get-Command claude -ErrorAction SilentlyContinue) {
        Step 'remove the MCP entry from Claude Code' {
            try { claude mcp remove winhands --scope user 2>$null | Out-Null } catch { }
        }
    }
    Step "remove the skill folder $SkillDir" { Remove-Item $SkillDir -Recurse -Force -ErrorAction SilentlyContinue }
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
