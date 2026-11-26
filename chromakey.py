"""绿幕视频抠图 -> 透明背景视频（依赖 ffmpeg，无需额外 Python 包）。

用法:
  python chromakey.py 输入.mp4                      # 输出 输入.webm（VP9 + alpha）
  python chromakey.py 绿幕目录 -o webm              # 批量处理目录下所有视频
  python chromakey.py a.mp4 --format mov           # 输出 ProRes 4444 .mov（剪辑软件用）
  python chromakey.py a.mp4 --similarity 0.12 --blend 0.08 --color 0x00FF00

参数调节:
  --similarity  越大抠得越多（绿边残留就调大，主体被抠穿就调小），默认 0.15
  --blend       边缘羽化程度，默认 0.05
  --color       幕布颜色，默认 auto：取首帧左上角像素平均色
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".flv"}
# 打包成 exe 后，程序目录是 exe 所在目录；源码运行时是脚本所在目录
APP_DIR = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent


def find_tool(name: str) -> str:
    """优先使用程序目录 bin/ 下自带的 ffmpeg，找不到再用系统 PATH。"""
    exe = name + (".exe" if sys.platform == "win32" else "")
    local = APP_DIR / "bin" / exe
    if local.exists():
        return str(local)
    found = shutil.which(name)
    if not found:
        raise SystemExit(f"找不到 {name}，请把 {exe} 放到 {APP_DIR / 'bin'} 目录")
    return found


FFMPEG = find_tool("ffmpeg")
FFPROBE = find_tool("ffprobe")


def detect_key_color(src: Path) -> str:
    """取首帧左上角 16x16 区域的平均色作为幕布颜色。"""
    cmd = [
        FFMPEG, "-v", "error", "-i", str(src), "-frames:v", "1",
        "-vf", "crop=16:16:0:0,scale=1:1:flags=area",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]
    data = subprocess.run(cmd, capture_output=True, check=True).stdout
    if len(data) < 3:
        raise RuntimeError(f"无法读取首帧: {src}")
    return "0x{:02X}{:02X}{:02X}".format(data[0], data[1], data[2])


def build_filter(color: str, similarity: float, blend: float,
                 despill: float, width: int | None) -> str:
    parts = []
    if width:
        parts.append(f"scale={width}:-2:flags=lanczos")
    parts.append(f"chromakey={color}:{similarity}:{blend}")
    if despill > 0:
        # 去除主体边缘反射的绿色溢色
        parts.append(f"despill=type=green:mix={despill}:expand=0")
    return ",".join(parts)


def encode_args(fmt: str, vf: str, crf: int, no_audio: bool) -> list[str]:
    """返回 -vf 及编码相关参数（不含输入输出）。"""
    if fmt == "webm":
        vf += ",format=yuva420p"
        codec = ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", str(crf),
                 "-row-mt", "1", "-auto-alt-ref", "0"]
    else:
        vf += ",format=yuva444p10le"
        codec = ["-c:v", "prores_ks", "-profile:v", "4444", "-alpha_bits", "16"]
    audio = ["-an"] if no_audio else (
        ["-c:a", "libopus"] if fmt == "webm" else ["-c:a", "pcm_s16le"])
    return ["-vf", vf, *codec, *audio]


def convert(src: Path, dst: Path, args) -> None:
    color = detect_key_color(src) if args.color == "auto" else args.color
    vf = build_filter(color, args.similarity, args.blend, args.despill, args.width)
    cmd = [FFMPEG, "-y", "-v", "error", "-stats", "-i", str(src),
           *encode_args(args.format, vf, args.crf, args.no_audio), str(dst)]
    print(f"[{src.name}] 幕布色 {color} -> {dst}")
    subprocess.run(cmd, check=True)


def main() -> None:
    p = argparse.ArgumentParser(description="绿幕视频抠图为透明背景视频")
    p.add_argument("input", type=Path, help="视频文件或目录")
    p.add_argument("-o", "--output", type=Path, help="输出目录（默认与输入同目录）")
    p.add_argument("--format", choices=["webm", "mov"], default="webm")
    p.add_argument("--color", default="auto", help="幕布颜色，如 0x00FF00，默认 auto")
    p.add_argument("--similarity", type=float, default=0.15)
    p.add_argument("--blend", type=float, default=0.05)
    p.add_argument("--despill", type=float, default=0.5, help="去溢色强度 0~1，0 关闭")
    p.add_argument("--width", type=int, help="输出宽度（等比缩放）")
    p.add_argument("--crf", type=int, default=30, help="webm 画质，越小越清晰")
    p.add_argument("--no-audio", action="store_true", help="去掉音轨")
    args = p.parse_args()

    if args.input.is_dir():
        files = sorted(f for f in args.input.iterdir() if f.suffix.lower() in VIDEO_EXTS)
    else:
        files = [args.input]
    if not files:
        sys.exit("没有找到视频文件")

    for src in files:
        out_dir = args.output or src.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        dst = out_dir / f"{src.stem}.{args.format}"
        if dst.resolve() == src.resolve():
            dst = out_dir / f"{src.stem}_alpha.{args.format}"
        convert(src, dst, args)


if __name__ == "__main__":
    main()
