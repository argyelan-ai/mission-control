# Mission Control - Windows bootstrapper (WSL2 path).
#
# Running MC natively on Windows is not supported (the stack needs POSIX
# bind mounts). The supported path is WSL2 - and this script gets you there
# in as few clicks as possible:
#
#   1. Checks Windows version, virtualization, WSL2, Ubuntu distro, Docker
#      Desktop (with WSL integration).
#   2. Installs what is missing (WSL2 needs one reboot - the script is
#      idempotent, just run it again after).
#   3. Runs the standard Linux installer inside WSL and opens the browser.
#
#   .\setup.ps1              # do it
#   .\setup.ps1 -CheckOnly   # report only, change NOTHING
#
# Docs: docs/setup/windows.md

param(
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"
$script:actions = @()

function Step($msg)  { Write-Host "==> $msg" -ForegroundColor Cyan }
function Ok($msg)    { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Todo($msg)  { Write-Host "  [->] $msg" -ForegroundColor Yellow; $script:actions += $msg }
function Fail($msg)  { Write-Host "  [X] $msg" -ForegroundColor Red }

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

# -- 1. Windows version ------------------------------------------------------
Step "Windows version"
$build = [System.Environment]::OSVersion.Version.Build
if ($build -ge 19041) {
    Ok "Build $build (WSL2-capable)"
} else {
    Fail "Windows build $build is too old for WSL2 (need 19041+ / Windows 10 2004). Update Windows first."
    exit 1
}

# -- 2. Virtualization -------------------------------------------------------
Step "Hardware virtualization"
try {
    $cpu = Get-CimInstance Win32_Processor
    if ($cpu.VirtualizationFirmwareEnabled -contains $true) {
        Ok "Enabled in firmware"
    } else {
        # On VMs this property is often False/absent although nested virt works -
        # Hyper-V present is the better signal there.
        $hv = (Get-CimInstance Win32_ComputerSystem).HypervisorPresent
        if ($hv) { Ok "Hypervisor present (VM with nested virtualization or Hyper-V active)" }
        else { Todo "Enable virtualization (BIOS/UEFI: VT-x/AMD-V; on a VM: nested virtualization - ESXi: 'Expose hardware assisted virtualization', Hyper-V: Set-VMProcessor -ExposeVirtualizationExtensions `$true)" }
    }
} catch {
    Todo "Could not query virtualization state - if WSL2 fails to start, check BIOS/hypervisor settings"
}

# -- 3. WSL2 -----------------------------------------------------------------
Step "WSL2"
$wslOk = $false
try {
    $null = wsl.exe --status 2>$null
    if ($LASTEXITCODE -eq 0) { $wslOk = $true }
} catch {}
if ($wslOk) {
    Ok "WSL is installed"
    $distros = (wsl.exe --list --quiet) -split "`r?`n" | Where-Object { $_ -ne "" }
    if ($distros -match "Ubuntu") {
        Ok "Ubuntu distro present"
    } else {
        Todo "Install the Ubuntu distro (wsl --install -d Ubuntu)"
        if (-not $CheckOnly) {
            wsl.exe --install -d Ubuntu
            Write-Host "`nUbuntu is installing. If Windows asks for a reboot: reboot, then run .\setup.ps1 again." -ForegroundColor Yellow
            exit 0
        }
    }
} else {
    Todo "Install WSL2 (wsl --install) - needs Administrator and ONE reboot"
    if (-not $CheckOnly) {
        if (-not (Test-Admin)) {
            Fail "This step needs an elevated shell. Right-click PowerShell -> 'Run as administrator', then run .\setup.ps1 again."
            exit 1
        }
        wsl.exe --install -d Ubuntu
        Write-Host "`nWSL2 is installing. REBOOT when prompted, then run .\setup.ps1 again - it continues where it left off." -ForegroundColor Yellow
        exit 0
    }
}

# -- 4. Docker Desktop -------------------------------------------------------
Step "Docker Desktop (WSL2 backend)"
$dockerExe = Join-Path $Env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
$dockerCli = Get-Command docker -ErrorAction SilentlyContinue
if ($dockerCli) {
    Ok "docker CLI found ($($dockerCli.Source))"
    try {
        $null = docker info 2>$null
        if ($LASTEXITCODE -eq 0) { Ok "Docker daemon reachable" }
        else { Todo "Start Docker Desktop (daemon not reachable) and enable Settings -> Resources -> WSL integration for Ubuntu" }
    } catch { Todo "Start Docker Desktop and enable WSL integration for Ubuntu" }
} elseif (Test-Path $dockerExe) {
    Todo "Docker Desktop is installed but not on PATH - start it once and enable WSL integration"
} else {
    Todo "Install Docker Desktop (winget install -e --id Docker.DockerDesktop), then enable Settings -> Resources -> WSL integration for Ubuntu"
    if (-not $CheckOnly) {
        $answer = Read-Host "Install Docker Desktop now via winget? [y/N]"
        if ($answer -match '^[yY]') {
            winget install -e --id Docker.DockerDesktop --accept-package-agreements --accept-source-agreements
            Write-Host "Start Docker Desktop once (it finishes its WSL setup on first run), then run .\setup.ps1 again." -ForegroundColor Yellow
            exit 0
        } else {
            Fail "Docker Desktop is required. Install it, then run .\setup.ps1 again."
            exit 1
        }
    }
}

# -- 5. Install Mission Control inside WSL -----------------------------------
Step "Mission Control (inside WSL/Ubuntu)"
if ($CheckOnly) {
    if ($script:actions.Count -eq 0) {
        Ok "Everything present - a plain .\setup.ps1 would now run the installer inside WSL and open http://localhost"
    } else {
        Write-Host "`nCheck-only mode - nothing was changed. Open actions:" -ForegroundColor Yellow
        $script:actions | ForEach-Object { Write-Host "  * $_" }
    }
    exit 0
}

Write-Host "Running the standard installer inside Ubuntu (interactive - it asks 2-3 questions)..." -ForegroundColor Cyan
wsl.exe -d Ubuntu -- bash -lc "curl -fsSL https://raw.githubusercontent.com/argyelan-ai/mission-control/main/install.sh | bash"
if ($LASTEXITCODE -ne 0) {
    Fail "The installer inside WSL reported an error - scroll up for its message."
    exit 1
}

Start-Process "http://localhost"
Write-Host "`nMission Control is up - the browser should show it at http://localhost (WSL2 forwards the port)." -ForegroundColor Green
