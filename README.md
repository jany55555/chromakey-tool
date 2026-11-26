# 绿幕抠图工具

本地网页工具：拖入绿幕视频，实时预览抠图效果，导出透明背景的 WebM（VP9 alpha）或 MOV（ProRes 4444）。

「堆叠 MP4（手机网页）」页签导出 640×720 H.264 MP4（上半彩色、下半 alpha 灰度），网页用 WebGL 还原透明，解决手机浏览器播放 WebM 变黑底的问题。

## 源码运行

需要 Python 3.10+ 和 ffmpeg（放在 PATH，或放到 `bin/ffmpeg.exe`、`bin/ffprobe.exe`）。

```powershell
python app.py
```

命令行批量处理：

```powershell
python chromakey.py 视频目录 -o 输出目录 --format mov
python stacked.py 绿幕.mp4 输出.mp4 --solid 40 --clear 110   # 堆叠 MP4
```

堆叠 MP4 需要 numpy（`python -m pip install numpy`）。

## 打包分发（Windows）

```powershell
python -m pip install pyinstaller
.\build.ps1                       # 或 .\build.ps1 -FfmpegDir C:\ffmpeg\bin
```

输出 `dist\绿幕抠图工具.zip`，解压后双击 exe 即可，用户说明见 `使用说明.txt`。

## 文件

- `app.py`：本地 HTTP 服务（只监听 127.0.0.1）
- `chromakey.py`：抠图滤镜和编码参数，也可单独作为命令行使用
- `stacked.py`：堆叠 MP4 的抠像、去噪、颜色外扩和编码（纯 numpy，不依赖 OpenCV），也可单独作为命令行使用
- `static/index.html`：页面
