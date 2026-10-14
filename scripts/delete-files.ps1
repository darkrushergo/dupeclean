# delete-files.ps1 —— 根据 dupeclean 生成的删除名单 CSV 删除文件（Windows）
#
# 危险操作：默认只“演练”（列出将删除的文件），必须显式加 -Yes 才真正删除。
#
# 用法（在 PowerShell 里）：
#   .\delete-files.ps1 -Map "/run/media/user/DISK_A=E:\" -Path .\reports\r1        # 演练
#   .\delete-files.ps1 -Yes -Map "/run/media/user/DISK_A=E:\" -Path .\reports\r1   # 真删
#
# 也可以直接双击 delete-files.bat，然后把 CSV 或目录拖进去。
#
# 参数：
#   -Yes            真正执行删除（不加则只显示将删除的文件）
#   -Map "OLD=NEW"  路径前缀替换（可多个；把扫描时的 Linux 路径映射成 Windows 盘符/路径）
#   -Path <...>     删除名单 CSV，或包含它的目录（目录会自动找 delete-list-*.csv）
#
# 说明：
#   * 处理 action=delete（删文件）与 action=rmdir（仅删空目录，不递归）。
#   * 删除不可逆！请先演练、核对名单、保留至少一份并备份。详见 DISCLAIMER.md。
#   * 自动读取 CSV 同目录或当前目录下的 delete-map.txt（每行 OLD=NEW）。
#   * 逻辑与 Linux 版 scripts/delete-files.sh 一致。

[CmdletBinding()]
param(
  [switch]$Yes,
  [string[]]$Map = @(),
  [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
  [string[]]$Path = @()
)

$ErrorActionPreference = 'Continue'
$OutputEncoding = [System.Text.Encoding]::UTF8

$script:rawMaps = New-Object System.Collections.ArrayList
foreach ($m in $Map) { [void]$script:rawMaps.Add($m) }

function Add-MapFile([string]$dir) {
  if ([string]::IsNullOrEmpty($dir)) { return }
  $f = Join-Path $dir 'delete-map.txt'
  if (Test-Path -LiteralPath $f) {
    foreach ($line in (Get-Content -LiteralPath $f -Encoding UTF8)) {
      $t = $line.Trim()
      if ($t -eq '' -or $t.StartsWith('#') -or -not $t.Contains('=')) { continue }
      [void]$script:rawMaps.Add($t)
    }
  }
}

$script:maps = @()
foreach ($m in $script:rawMaps) {
  $idx = $m.IndexOf('=')
  if ($idx -gt 0) {
    $old = $m.Substring(0, $idx).Trim()
    $new = $m.Substring($idx + 1).Trim()
    if ($old -ne '') {
      $script:maps += [pscustomobject]@{ Old = $old; New = $new; Len = $old.Length }
    }
  }
}
$script:maps = @($script:maps | Sort-Object -Property Len -Descending)

function Remap([string]$p) {
  foreach ($mp in $script:maps) {
    $oldTrim = $mp.Old.TrimEnd('/', '\')
    if (($p -eq $mp.Old) -or $p.StartsWith($oldTrim + '/') -or $p.StartsWith($oldTrim + '\')) {
      $tail = $p.Substring($mp.Old.Length)
      return ($mp.New.TrimEnd('/', '\') + ($tail -replace '/', '\'))
    }
  }
  return ($p -replace '/', '\')
}

if ($Path.Count -eq 0) {
  Write-Host "用法: delete-files.ps1 [-Yes] [-Map OLD=NEW]... <删除名单.csv 或 目录>..."
  Write-Host "提示: 也可以双击 delete-files.bat，并把 CSV 或目录拖到它上面。"
  exit 2
}

$csvs = New-Object System.Collections.ArrayList
foreach ($t in $Path) {
  if (Test-Path -LiteralPath $t -PathType Container) {
    Add-MapFile $t
    $found = @(Get-ChildItem -LiteralPath $t -Filter 'delete-list-*.csv' -File -ErrorAction SilentlyContinue)
    if ($found.Count -eq 0) {
      $found = @(Get-ChildItem -LiteralPath $t -Filter 'delete-list.csv' -File -ErrorAction SilentlyContinue)
    }
    if ($found.Count -eq 0) { Write-Host "[警告] $t 里没有 delete-list-*.csv" }
    foreach ($f in $found) { [void]$csvs.Add($f.FullName) }
  }
  else {
    $full = (Resolve-Path -LiteralPath $t -ErrorAction SilentlyContinue).Path
    if (-not $full) { Write-Host "[跳过] 找不到: $t"; continue }
    Add-MapFile (Split-Path -Parent $full)
    [void]$csvs.Add($full)
  }
}

Write-Host ("=" * 74)
Write-Host "  DANGER: this script permanently deletes files with no recovery."
Write-Host "  External drives / USB / NAS usually have no trash; deletion is irreversible."
Write-Host ("  Mode: " + $(if ($Yes) { "REAL DELETE (-Yes)  >>> will permanently delete <<<" } else { "DRY-RUN (list only)" }))
Write-Host "  Detection is heuristic and can be wrong. Dry-run, review, keep one copy, back up first."
Write-Host "  Full disclaimer: DISCLAIMER.md. You assume all risk."
Write-Host ("=" * 74)
Write-Host ""

$total = 0; $will = 0; $done = 0; $missing = 0; $skipped = 0; $failed = 0
foreach ($c in $csvs) {
  if (-not (Test-Path -LiteralPath $c)) { Write-Host "[跳过] 找不到 CSV: $c"; continue }
  try {
    $rows = Import-Csv -LiteralPath $c -Encoding UTF8
  }
  catch {
    Write-Host "[错误] 读取 $c 失败: $_"
    continue
  }
  foreach ($row in $rows) {
    $act = ($row.action + '').Trim().ToLower()
    if ($act -notin @('delete', 'rmdir', 'delete-dir', 'rmdir-dir')) { continue }
    $raw = ($row.path + '').Trim()
    if ($raw -eq '' -or $raw -eq '/' -or $raw -eq '\') { continue }
    $total++
    $p = Remap $raw
    if (-not (Test-Path -LiteralPath $p)) { $missing++; continue }
    if ($act -in @('rmdir', 'rmdir-dir', 'delete-dir')) {
      # 只删除“空目录”（不递归）
      if (-not (Test-Path -LiteralPath $p -PathType Container)) { $skipped++; continue }
      $empty = @(Get-ChildItem -LiteralPath $p -Force -ErrorAction SilentlyContinue).Count -eq 0
      if (-not $empty) {
        $skipped++
        Write-Host "[跳过] 目录非空，保留: $p"
        continue
      }
      $will++
      if ($Yes) {
        try {
          Remove-Item -LiteralPath $p -Force -ErrorAction Stop
          $done++
          Write-Host "[删除空目录] $p"
        }
        catch {
          $failed++
          Write-Host "[失败] $p : $_"
        }
      }
      else {
        Write-Host "[将删除空目录] $p"
      }
      continue
    }
    if (Test-Path -LiteralPath $p -PathType Container) {
      $skipped++
      Write-Host "[跳过] 是目录，不删除: $p"
      continue
    }
    $will++
    if ($Yes) {
      try {
        Remove-Item -LiteralPath $p -Force -ErrorAction Stop
        $done++
        Write-Host "[删除] $p"
      }
      catch {
        $failed++
        Write-Host "[失败] $p : $_"
      }
    }
    else {
      Write-Host "[将删除] $p"
    }
  }
}

Write-Host ""
if ($Yes) {
  Write-Host "完成：删除 $done 个，失败 $failed 个，缺失 $missing 个，跳过(目录) $skipped 个。"
}
else {
  Write-Host "演练模式（未删除任何文件）：待删除 $will 个，缺失 $missing 个，跳过(目录) $skipped 个。"
  Write-Host "确认无误后，加上 -Yes 再运行一次即可真正删除。"
}
