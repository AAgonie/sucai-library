[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$SourceRoot,
    [Parameter(Mandatory)]
    [string]$DestinationRoot,
    [switch]$Execute
)

$ErrorActionPreference = 'Stop'

function Get-RelativeFileMap {
    param([Parameter(Mandatory)][string]$Root)

    $rootPath = [System.IO.Path]::GetFullPath($Root).TrimEnd('\')
    $files = @{}
    Get-ChildItem -LiteralPath $rootPath -File -Recurse -Force | ForEach-Object {
        $relativePath = $_.FullName.Substring($rootPath.Length).TrimStart('\')
        $files[$relativePath] = $_
    }
    return $files
}

function Get-DirectoryBytes {
    param([Parameter(Mandatory)][hashtable]$FileMap)
    return [long](($FileMap.Values | Measure-Object -Property Length -Sum).Sum)
}

function Remove-MigrationMetadata {
    param([Parameter(Mandatory)][hashtable]$FileMap)

    @($FileMap.Keys | Where-Object { $_ -like '.photo-web-migration\*' }) | ForEach-Object {
        [void]$FileMap.Remove($_)
    }
}

function Test-FileMap {
    param(
        [Parameter(Mandatory)][hashtable]$SourceFiles,
        [Parameter(Mandatory)][hashtable]$DestinationFiles
    )

    $missing = New-Object System.Collections.Generic.List[string]
    $sizeMismatch = New-Object System.Collections.Generic.List[string]
    $hashMismatch = New-Object System.Collections.Generic.List[string]
    $extra = New-Object System.Collections.Generic.List[string]

    foreach ($relativePath in $SourceFiles.Keys) {
        if (-not $DestinationFiles.ContainsKey($relativePath)) {
            $missing.Add($relativePath)
            continue
        }

        $sourceFile = $SourceFiles[$relativePath]
        $destinationFile = $DestinationFiles[$relativePath]
        if ($sourceFile.Length -ne $destinationFile.Length) {
            $sizeMismatch.Add($relativePath)
            continue
        }

        $sourceHash = (Get-FileHash -LiteralPath $sourceFile.FullName -Algorithm SHA256).Hash
        $destinationHash = (Get-FileHash -LiteralPath $destinationFile.FullName -Algorithm SHA256).Hash
        if ($sourceHash -ne $destinationHash) {
            $hashMismatch.Add($relativePath)
        }
    }

    foreach ($relativePath in $DestinationFiles.Keys) {
        if (-not $SourceFiles.ContainsKey($relativePath)) {
            $extra.Add($relativePath)
        }
    }

    return [PSCustomObject]@{
        Missing = @($missing)
        SizeMismatch = @($sizeMismatch)
        HashMismatch = @($hashMismatch)
        Extra = @($extra)
        IsValid = ($missing.Count -eq 0 -and $sizeMismatch.Count -eq 0 -and $hashMismatch.Count -eq 0 -and $extra.Count -eq 0)
    }
}

if (-not (Test-Path -LiteralPath $SourceRoot -PathType Container)) {
    throw "无法访问 NAS 源目录：$SourceRoot。请在已连接 NAS 的服务机运行本工具。"
}

$sourceFiles = Get-RelativeFileMap -Root $SourceRoot
$sourceBytes = Get-DirectoryBytes -FileMap $sourceFiles
$sourceMegabytes = [Math]::Round($sourceBytes / 1MB, 2)
$requiredFreeBytes = [Math]::Ceiling($sourceBytes * 1.2)
$destinationFullPath = [System.IO.Path]::GetFullPath($DestinationRoot)
$driveName = [System.IO.Path]::GetPathRoot($destinationFullPath).TrimEnd('\').TrimEnd(':')
$drive = Get-PSDrive -Name $driveName

Write-Host ''
Write-Host 'NAS 临时本地副本：预检结果' -ForegroundColor Cyan
Write-Host "源目录：$SourceRoot"
Write-Host "目标目录：$destinationFullPath"
Write-Host "源文件：$($sourceFiles.Count) 个，$sourceMegabytes MB"
Write-Host ("目标磁盘可用：{0:N2} GB；建议至少保留：{1:N2} GB" -f ($drive.Free / 1GB), ($requiredFreeBytes / 1GB))

if ($drive.Free -lt $requiredFreeBytes) {
    throw '目标磁盘可用空间不足。未开始复制。'
}

if (-not $Execute) {
    Write-Host ''
    Write-Host '这是预检，未复制任何文件。确认后在服务机运行同一脚本并附加 -Execute。' -ForegroundColor Yellow
    exit 0
}

$stateRoot = Join-Path $destinationFullPath '.photo-web-migration'
$statePath = Join-Path $stateRoot 'nas-copy-state.json'
$logPath = Join-Path $stateRoot 'robocopy.log'
$verificationPath = Join-Path $stateRoot 'verification.json'

if ((Test-Path -LiteralPath $destinationFullPath -PathType Container) -and
    ((Get-ChildItem -LiteralPath $destinationFullPath -Force | Measure-Object).Count -gt 0) -and
    (-not (Test-Path -LiteralPath $statePath -PathType Leaf))) {
    throw "目标目录 $destinationFullPath 已有内容但不是本工具创建的迁移副本。为保护现有文件，已拒绝继续。请改用一个空目录。"
}

New-Item -ItemType Directory -Force -Path $stateRoot | Out-Null

[PSCustomObject]@{
    source_root = $SourceRoot
    destination_root = $destinationFullPath
    started_at = (Get-Date).ToString('o')
    source_file_count = $sourceFiles.Count
    source_bytes = $sourceBytes
    mode = 'copy-and-sha256-verify'
    source_delete = $false
} | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding utf8

Write-Host ''
Write-Host '开始仅复制。不会移动、删除或修改 NAS 原文件。' -ForegroundColor Cyan
& robocopy $SourceRoot $destinationFullPath /E /Z /R:2 /W:2 /COPY:DAT /DCOPY:DAT /FFT /NP /TEE /LOG:$logPath
$robocopyExitCode = $LASTEXITCODE
if ($robocopyExitCode -gt 7) {
    throw "Robocopy 失败，退出码：$robocopyExitCode。请查看日志：$logPath"
}

Write-Host ''
Write-Host '复制完成，开始逐文件 SHA-256 校验。' -ForegroundColor Cyan
$destinationFiles = Get-RelativeFileMap -Root $destinationFullPath
Remove-MigrationMetadata -FileMap $destinationFiles
$verification = Test-FileMap -SourceFiles $sourceFiles -DestinationFiles $destinationFiles

[PSCustomObject]@{
    completed_at = (Get-Date).ToString('o')
    robocopy_exit_code = $robocopyExitCode
    source_file_count = $sourceFiles.Count
    destination_file_count = $destinationFiles.Count
    missing = @($verification.Missing)
    size_mismatch = @($verification.SizeMismatch)
    hash_mismatch = @($verification.HashMismatch)
    extra = @($verification.Extra)
    verified = $verification.IsValid
} | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $verificationPath -Encoding utf8

if (-not $verification.IsValid) {
    Write-Host "校验未通过。缺失 $($verification.Missing.Count) 个，大小不一致 $($verification.SizeMismatch.Count) 个，哈希不一致 $($verification.HashMismatch.Count) 个，额外文件 $($verification.Extra.Count) 个。" -ForegroundColor Red
    throw "副本校验失败。NAS 原文件和数据库均未改动。详情：$verificationPath"
}

Write-Host ''
Write-Host "复制与 SHA-256 校验均通过。状态与日志位于：$stateRoot" -ForegroundColor Green
Write-Host 'NAS 原文件未删除，数据库路径也未变更。'
