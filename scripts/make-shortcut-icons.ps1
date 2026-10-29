# make-shortcut-icons.ps1 — 生成桌面快捷方式图标（start.ico / stop.ico）
# 纯 PowerShell + System.Drawing，无外部依赖：
#   1) 绘制 256x256 PNG（圆角方块底 + 白色图形）
#   2) 手动封装 ICO 容器（ICONDIR + ICONDIRENTRY + PNG 字节，Vista+ 原生支持 PNG 压缩条目）
# 用法：powershell -NoProfile -ExecutionPolicy Bypass -File make-shortcut-icons.ps1 [-OutDir <dir>]
param([string]$OutDir = "d:\self_coding\ontology_platform\installer\icons")

Add-Type -AssemblyName System.Drawing

function New-RoundedRectPath([float]$x, [float]$y, [float]$w, [float]$h, [float]$r) {
    $p = New-Object System.Drawing.Drawing2D.GraphicsPath
    $p.AddArc($x, $y, $r, $r, 180, 90)
    $p.AddArc($x + $w - $r, $y, $r, $r, 270, 90)
    $p.AddArc($x + $w - $r, $y + $h - $r, $r, $r, 0, 90)
    $p.AddArc($x, $y + $h - $r, $r, $r, 90, 90)
    $p.CloseFigure()
    return $p
}

function ConvertTo-Ico([string]$pngPath, [string]$icoPath, [int]$size) {
    $png = [System.IO.File]::ReadAllBytes($pngPath)
    $ms = New-Object System.IO.MemoryStream
    $bw = New-Object System.IO.BinaryWriter($ms)
    $bw.Write([uint16]0)      # reserved
    $bw.Write([uint16]1)      # type: icon
    $bw.Write([uint16]1)      # count
    $bw.Write([byte]0)        # width (0 = 256)
    $bw.Write([byte]0)        # height (0 = 256)
    $bw.Write([byte]0)        # palette colors
    $bw.Write([byte]0)        # reserved
    $bw.Write([uint16]1)      # color planes
    $bw.Write([uint16]32)     # bits per pixel
    $bw.Write([uint32]$png.Length)
    $bw.Write([uint32]22)     # image data offset (6 + 16)
    $bw.Write($png)
    $bw.Flush()
    [System.IO.File]::WriteAllBytes($icoPath, $ms.ToArray())
    $bw.Dispose(); $ms.Dispose()
}

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$work = Join-Path $env:TEMP ("icongen_" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $work | Out-Null
$white = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::White)

# ---- start.ico：绿底 + 白色播放三角 ----
$bmp = New-Object System.Drawing.Bitmap(256, 256)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.SmoothingMode = 'AntiAlias'
$bg = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(255, 46, 158, 91))
$g.FillPath($bg, (New-RoundedRectPath 8 8 240 240 56))
$tri = New-Object System.Drawing.Drawing2D.GraphicsPath
$tri.AddPolygon([System.Drawing.PointF[]]@([System.Drawing.PointF]::new(100, 74), [System.Drawing.PointF]::new(100, 182), [System.Drawing.PointF]::new(192, 128)))
$g.FillPath($white, $tri)
$g.Dispose(); $bmp.Save((Join-Path $work "start.png"), [System.Drawing.Imaging.ImageFormat]::Png); $bmp.Dispose()

# ---- stop.ico：红底 + 白色停止方块 ----
$bmp = New-Object System.Drawing.Bitmap(256, 256)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.SmoothingMode = 'AntiAlias'
$bg = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(255, 217, 83, 79))
$g.FillPath($bg, (New-RoundedRectPath 8 8 240 240 56))
$g.FillPath($white, (New-RoundedRectPath 90 90 76 76 14))
$g.Dispose(); $bmp.Save((Join-Path $work "stop.png"), [System.Drawing.Imaging.ImageFormat]::Png); $bmp.Dispose()

foreach ($n in @('start', 'stop')) {
    ConvertTo-Ico (Join-Path $work "$n.png") (Join-Path $OutDir "$n.ico") 256
    Write-Output ("generated: {0}" -f (Join-Path $OutDir "$n.ico"))
}
Remove-Item $work -Recurse -Force

# 验证：System.Drawing 按 256 解码 ICO（容器 + PNG 条目非法会在此抛异常）
foreach ($n in @('start', 'stop')) {
    $ico = Join-Path $OutDir "$n.ico"
    $icon = New-Object System.Drawing.Icon($ico, 256, 256)
    Write-Output ("verify: {0} decoded={1}x{2} icoBytes={3}" -f $n, $icon.Width, $icon.Height, (Get-Item $ico).Length)
    $icon.Dispose()
}
Write-Output "--- icons done ---"
