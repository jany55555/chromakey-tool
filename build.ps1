# 打包成 Windows 免安装版：dist\绿幕抠图工具\ 和 dist\绿幕抠图工具.zip
# 依赖：python -m pip install pyinstaller；ffmpeg/ffprobe 在 PATH 或通过 -FfmpegDir 指定
param([string]$FfmpegDir = "")
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$name = "绿幕抠图工具"

if (-not $FfmpegDir) { $FfmpegDir = Split-Path (Get-Command ffmpeg).Source }
foreach ($t in "ffmpeg.exe", "ffprobe.exe") {
    if (-not (Test-Path "$FfmpegDir\$t")) { throw "找不到 $FfmpegDir\$t" }
}

python -m PyInstaller --noconfirm --clean --onedir --console --name $name `
    --add-data "static;static" --distpath dist --workpath build app.py
if ($LASTEXITCODE) { throw "PyInstaller 失败" }

$out = "dist\$name"
New-Item -ItemType Directory -Force "$out\bin" | Out-Null
Copy-Item "$FfmpegDir\ffmpeg.exe", "$FfmpegDir\ffprobe.exe" "$out\bin\"
Copy-Item "使用说明.txt" $out

$zip = "dist\$name.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path $out -DestinationPath $zip -CompressionLevel Optimal
"完成: $((Resolve-Path $zip).Path)  ($([math]::Round((Get-Item $zip).Length / 1MB, 1)) MB)"
