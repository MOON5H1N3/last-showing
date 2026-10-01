# Last Showing updater for Windows.
#
# Run from this folder in PowerShell:   .\update.ps1
# (If Windows blocks it, run once:  Set-ExecutionPolicy -Scope CurrentUser RemoteSigned)
#
# It downloads the latest version from GitHub, replaces the program files, and rebuilds the container.
# Your .env, your data folder (database, backups, Letterboxd export) and your docker-compose.yml are never touched.

param(
    [string]$Repo = "MOON5H1N3/last-showing",
    [string]$Branch = "main"
)
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

if ($Repo -like "__*") { Write-Host "Set the GitHub repository at the top of update.ps1 first." -ForegroundColor Red; exit 1 }
if (-not (Test-Path ".env")) { Write-Host "No .env here. Run this from your Last Showing folder." -ForegroundColor Red; exit 1 }

$before = if (Test-Path "VERSION") { (Get-Content "VERSION" -Raw).Trim() } else { "unknown" }
$work = Join-Path $PSScriptRoot ".update"
if (Test-Path $work) { Remove-Item $work -Recurse -Force }
New-Item -ItemType Directory -Path $work | Out-Null

Write-Host "Downloading the latest Last Showing from github.com/$Repo ..."
$zip = Join-Path $work "latest.zip"
Invoke-WebRequest -Uri "https://github.com/$Repo/archive/refs/heads/$Branch.zip" -OutFile $zip -UseBasicParsing
Expand-Archive -Path $zip -DestinationPath $work
$src = Get-ChildItem $work -Directory | Select-Object -First 1

$after = (Get-Content (Join-Path $src.FullName "VERSION") -Raw).Trim()
if ($after -eq $before) {
    Write-Host "Already up to date ($before)." -ForegroundColor Green
    Remove-Item $work -Recurse -Force
    exit 0
}

# program folders are replaced whole, so files removed upstream disappear here too
foreach ($dir in @("app", "tests", ".github")) {
    if (Test-Path $dir) { Remove-Item $dir -Recurse -Force }
    $from = Join-Path $src.FullName $dir
    if (Test-Path $from) { Copy-Item $from -Destination $dir -Recurse }
}
foreach ($file in @("Dockerfile", "requirements.txt", "README.md", "VERSION", ".env.example", ".dockerignore", ".gitignore", "update.ps1")) {
    $from = Join-Path $src.FullName $file
    if (Test-Path $from) { Copy-Item $from -Destination $file -Force }
}
# docker-compose.yml is yours (ports, paths): only added if missing, and you're told if the template changed
$newCompose = Join-Path $src.FullName "docker-compose.yml"
if (-not (Test-Path "docker-compose.yml")) {
    Copy-Item $newCompose -Destination "docker-compose.yml"
} elseif ((Get-FileHash $newCompose).Hash -ne (Get-FileHash "docker-compose.yml").Hash) {
    Copy-Item $newCompose -Destination "docker-compose.example.yml" -Force
    Write-Host "Note: the template docker-compose.yml changed. Compare docker-compose.example.yml with yours." -ForegroundColor Yellow
}
Remove-Item $work -Recurse -Force

Write-Host "Updated $before -> $after. Rebuilding ..."
docker compose up -d --build
if ($LASTEXITCODE -ne 0) { Write-Host "The rebuild failed; the output above says why." -ForegroundColor Red; exit 1 }
Write-Host "Done. Last Showing $after is running. Follow the first refresh with:  docker logs -f last-showing" -ForegroundColor Green
