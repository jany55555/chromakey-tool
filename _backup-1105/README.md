# 绿幕抠图工具

本地网页工具：拖入绿幕视频，实时预览抠图效果，导出透明背景的 WebM（VP9 alpha）或 MOV（ProRes 4444）。

## 源码运行

需要 Python 3.10+ 和 ffmpeg（放在 PATH，或放到 `bin/ffmpeg.exe`、`bin/ffprobe.exe`）。

```powershell
python app.py
```

命令行批量处理：

```powershell
python chromakey.py 视频目录 -o 输出目录 --format mov
```

## 打包分发（Windows）

```powershell
python -m pip install pyinstaller
.\build.ps1                       # 或 .\build.ps1 -FfmpegDir C:\ffmpeg\bin
```

输出 `dist\绿幕抠图工具.zip`，解压后双击 exe 即可，用户说明见 `使用说明.txt`。

## 文件

- `app.py`：本地 HTTP 服务（只监听 127.0.0.1）
- `chromakey.py`：抠图滤镜和编码参数，也可单独作为命令行使用
- `static/index.html`：页面
