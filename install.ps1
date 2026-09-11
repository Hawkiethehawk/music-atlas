#Requires -Version 5.1
<#
.SYNOPSIS
    Music Atlas 一键部署：定位 Python，然后执行内置的 `atlas setup`。

.DESCRIPTION
    安装流程本身就是 `python atlas.py setup`，它会：
      1. 检查 Python / Node.js / npm / 依赖 / Playwright Chromium / 配置；
      2. 安装缺失依赖（tools 与 web 的 npm ci、Playwright Chromium）；
      3. 注册 `atlas` 命令（用户级 bin 目录 + 用户 PATH，幂等）。

    安装完成后新开一个终端即可直接使用 `atlas start`。

.PARAMETER CheckOnly
    只检查环境，不做任何安装或注册。

.PARAMETER SkipNodeDeps
    跳过 npm ci（适合已经装好 node_modules 的机器）。

.PARAMETER SkipBrowser
    跳过 Playwright Chromium 下载（不需要 Apple Music 导出时可用）。

.PARAMETER SkipRegister
    跳过 `atlas` 命令注册。

.EXAMPLE
    pwsh -File install.ps1
    pwsh -File install.ps1 -CheckOnly
#>
[CmdletBinding()]
param(
    [switch]$CheckOnly,
    [switch]$SkipNodeDeps,
    [switch]$SkipBrowser,
    [switch]$SkipRegister
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Write-Host "=== Music Atlas 安装程序 ===" -ForegroundColor Cyan
Write-Host "仓库目录：$Root"

$python = $null
foreach ($candidate in @("python", "python3", "py")) {
    $command = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($command) {
        $python = $command.Source
        break
    }
}

if (-not $python) {
    Write-Host "❌ 找不到 Python。请安装 Python 3.10 或更高版本后重试。" -ForegroundColor Red
    exit 1
}
Write-Host "使用解释器：$python"

$setupArguments = @((Join-Path $Root "atlas.py"), "setup")
if ($CheckOnly) { $setupArguments += "--check-only" }
if ($SkipNodeDeps) { $setupArguments += "--skip-node-deps" }
if ($SkipBrowser) { $setupArguments += "--skip-browser" }
if ($SkipRegister) { $setupArguments += "--skip-register" }

Push-Location $Root
try {
    & $python @setupArguments
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

if ($exitCode -ne 0) {
    Write-Host "❌ 安装未完成（退出码 $exitCode）。" -ForegroundColor Red
    exit $exitCode
}

Write-Host "✅ 安装完成。新开一个终端后可直接运行：atlas start" -ForegroundColor Green
exit 0
