[CmdletBinding()]
param(
    [ValidateSet('full', 'auto', 'follow', 'catalog', 'media', 'sync', 'metadata', 'tooling', 'web', 'checks', 'core', 'packaging')]
    [string]$Scope = 'auto',
    [switch]$Fresh,
    [string[]]$Focus = @(),
    [string]$Base = 'master',
    [int]$ShardIndex = 0,
    [int]$ShardCount = 1,
    # 本机同时跑几个分片子进程：auto 按核数定；-Jobs 1 退回串行。CI 分片模式下运行器忽略它。
    [string]$Jobs = 'auto'
)

$ErrorActionPreference = 'Stop'
$WorktreeRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$GitCommonRaw = (& git -C $WorktreeRoot rev-parse --git-common-dir).Trim()
if ($LASTEXITCODE -ne 0 -or -not $GitCommonRaw) {
    throw '无法定位 Peach 主工作树。'
}

if ([IO.Path]::IsPathRooted($GitCommonRaw)) {
    $GitCommon = (Resolve-Path -LiteralPath $GitCommonRaw).Path
} else {
    $GitCommon = (Resolve-Path -LiteralPath (Join-Path $WorktreeRoot $GitCommonRaw)).Path
}

$MainRoot = Split-Path -Parent $GitCommon
$Python = Join-Path $MainRoot '.venv\Scripts\python.exe'
$LocalPython = Join-Path $WorktreeRoot '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $LocalPython -PathType Leaf) { $Python = $LocalPython }
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Peach 主项目 venv 不存在：$Python"
}

$SourceRoot = (Resolve-Path -LiteralPath (Join-Path $WorktreeRoot 'src')).Path
$PreviousPythonPath = $env:PYTHONPATH
$PreviousPythonIoEncoding = $env:PYTHONIOENCODING
$PreviousPath = $env:PATH
$env:PYTHONPATH = $SourceRoot
$env:PYTHONIOENCODING = 'utf-8'

# Git for Windows 自带项目证书测试所需的 OpenSSL，但默认不会把 usr\bin 放进
# 用户 PATH。按已解析到的 git.exe 反推安装根目录，不写死某个盘符：装到 D: 的机器
# 与默认的 C:\Program Files\Git 一样能用。只对本次测试进程补齐，不修改系统或用户环境。
# 只要 Git 自带的那一份在就优先用它：PATH 上别的 openssl（例如 conda 那份）可能缺
# openssl.cnf，`test_certs` 拿它签不出 CA。
$GitExe = (Get-Command git.exe -ErrorAction SilentlyContinue).Source
if ($GitExe) {
    # `git.exe` 可能在 `cmd\` 也可能在 `mingw64\bin\`，逐级往上找带 `usr\bin\openssl.exe`
    # 的那一层，退固定两级会在后者上算成 `mingw64`。
    $GitRoot = Split-Path -Parent $GitExe
    foreach ($Step in 1..4) {
        $GitOpenSslDir = Join-Path $GitRoot 'usr\bin'
        if (Test-Path -LiteralPath (Join-Path $GitOpenSslDir 'openssl.exe') -PathType Leaf) {
            $env:PATH = "$GitOpenSslDir;$env:PATH"
            break
        }
        $Parent = Split-Path -Parent $GitRoot
        if (-not $Parent -or $Parent -eq $GitRoot) { break }
        $GitRoot = $Parent
    }
}

Push-Location $WorktreeRoot
try {
    & $Python -X utf8 scripts\test_environment.py $WorktreeRoot $MainRoot
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    $Python = $LocalPython
    $LoadedModule = (& $Python -c 'import peach; print(peach.__file__)').Trim()
    if ($LASTEXITCODE -ne 0 -or -not $LoadedModule) {
        throw '无法导入当前工作树中的 peach。'
    }
    $LoadedPath = (Resolve-Path -LiteralPath $LoadedModule).Path
    $ExpectedPrefix = $SourceRoot.TrimEnd('\') + '\'
    if (-not $LoadedPath.StartsWith($ExpectedPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "测试加载了错误源码：$LoadedPath；预期位于 $SourceRoot"
    }

    Write-Host "Peach source: $LoadedPath"
    $PeachTestExtra = @()
    if ($Fresh) { $PeachTestExtra += '--fresh' }
    foreach ($PeachFocusSelector in $Focus) { $PeachTestExtra += @('--focus', $PeachFocusSelector) }
    & $Python scripts\test_runner.py --scope $Scope --base $Base --shard-index $ShardIndex --shard-count $ShardCount --jobs $Jobs @PeachTestExtra
    exit $LASTEXITCODE
} finally {
    Pop-Location
    if ($null -eq $PreviousPythonIoEncoding) {
        Remove-Item Env:PYTHONIOENCODING -ErrorAction SilentlyContinue
    } else {
        $env:PYTHONIOENCODING = $PreviousPythonIoEncoding
    }
    if ($null -eq $PreviousPythonPath) {
        Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    } else {
        $env:PYTHONPATH = $PreviousPythonPath
    }
    $env:PATH = $PreviousPath
}
