# Mission Control — Windows setup (PowerShell)
#
# Mirror of setup.sh for PowerShell users running Docker Desktop (WSL2
# backend). Generates .env with cryptographically secure secrets.
# EXPERIMENTAL: the maintainers develop on macOS/Linux — reports welcome.
#
#   git clone https://github.com/argyelan-ai/mission-control.git
#   cd mission-control
#   .\setup.ps1
#   docker compose up -d          # pulls prebuilt images (or builds); migrations run automatically
#   start http://localhost

$ErrorActionPreference = "Stop"

Write-Host "🚀 Mission Control — Setup (Windows)" -ForegroundColor Cyan
Write-Host "================================"

function New-HexSecret([int]$Bytes) {
    $buf = [byte[]]::new($Bytes)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($buf)
    ($buf | ForEach-Object { $_.ToString("x2") }) -join ""
}

function New-FernetKey {
    # 32 random bytes, url-safe base64 (what cryptography.Fernet expects)
    $buf = [byte[]]::new(32)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($buf)
    [Convert]::ToBase64String($buf).Replace('+', '-').Replace('/', '_')
}

if (Test-Path ".env") {
    Write-Host "✅ .env already exists, skipping generation"
} else {
    if (-not (Test-Path ".env.example")) {
        Write-Error ".env.example not found — run this from the repo root."
    }
    Copy-Item ".env.example" ".env"

    $replacements = @{
        "LOCAL_AUTH_TOKEN"       = New-HexSecret 32
        "DB_PASSWORD"            = New-HexSecret 16
        "JWT_SECRET_KEY"         = New-HexSecret 32
        "REDIS_PASSWORD"         = New-HexSecret 16
        "SECRETS_ENCRYPTION_KEY" = New-FernetKey
        # Container-UID im WSL2-Backend; 1000 = mcuser im Image.
        "HOST_UID"               = "1000"
        "MC_REPO_PATH"           = (Get-Location).Path
    }

    $env_lines = Get-Content ".env"
    foreach ($key in $replacements.Keys) {
        $env_lines = $env_lines -replace "^$key=.*", "$key=$($replacements[$key])"
    }
    Set-Content ".env" $env_lines -Encoding utf8

    Write-Host "✅ .env created with generated secrets"
}

Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. docker compose up -d      # start the stack (migrations run automatically)"
Write-Host "  2. Open http://localhost and register the first admin."
Write-Host ""
Write-Host "Known Windows limitations: host-side agents (launchd) are macOS-only;"
Write-Host "cross-image runtime switching is untested on Windows."
Write-Host "Full notes: docs/setup/windows.md"
