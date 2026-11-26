"""绿幕视频 -> 堆叠 Alpha MP4（上半彩色 + 下半透明度灰度，H.264），手机网页用 WebGL 还原透明。

输出固定 640x720（单半 640x360，16:9）、24fps、无音轨、BT.709。
非 16:9 的视频等比缩放后用绿色补边，补出来的部分会被抠成透明，不会拉伸变形。

命令行:
  python stacked.py 输入.mp4 输出.mp4 [--solid 40] [--clear 110]
"""
import argparse
import subprocess
from pathlib import Path

import numpy as np

from chromakey import FFMPEG

W, H, FPS = 640, 360, 24
# 超采样倍数：先在 3 倍分辨率上抠像，再按透明度加权缩小，边缘得到抗锯齿过渡。
# 直接在 640x360 上按阈值抠，边缘只有 1px 的硬跳变，看起来就是锯齿。
SS = 3
SW, SH = W * SS, H * SS
FRAME_BYTES = SW * SH * 3
# 等比缩放进 1920x1080，空出来的地方补纯绿，随后被抠成透明
SCALE_VF = (f"fps={FPS},scale={SW}:{SH}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={SW}:{SH}:(ow-iw)/2:(oh-ih)/2:color=0x00FF00,setsar=1")


def key(frame: np.ndarray, solid: int, clear: int):
    """按「绿色程度」g = G - max(R, B) 算透明度：g<=solid 不透明，g>=clear 全透明，中间线性过渡。
    同时去绿溢色：绿通道压到不超过 max(R, B)，只影响偏绿的像素。"""
    f = frame.astype(np.int16)
    r, g, b = f[..., 0], f[..., 1], f[..., 2]
    rb = np.maximum(r, b)
    a = np.clip((clear - (g - rb)) / float(clear - solid), 0.0, 1.0)
    rgb = np.stack([r, np.minimum(g, rb), b], -1).astype(np.uint8)
    return rgb, (a * 255 + 0.5).astype(np.uint8)


def _box(img: np.ndarray, r: int, axis: int) -> np.ndarray:
    """沿一个轴做半径 r 的盒式平均（前缀和，耗时与半径无关），边缘按 0 填充。"""
    x = np.moveaxis(img, axis, 0)
    n = x.shape[0]
    # c[k] = 前 k 个元素之和（含开头补的 r 个 0）；窗口 [i-r, i+r] 之和 = c[i+2r+1] - c[i]
    p = np.concatenate([np.zeros((r + 1,) + x.shape[1:], np.float32), x,
                        np.zeros((r,) + x.shape[1:], np.float32)])
    c = np.cumsum(p, axis=0, dtype=np.float32)
    out = (c[2 * r + 1:2 * r + 1 + n] - c[:n]) / (2 * r + 1)
    return np.moveaxis(out, 0, axis)


def _blur(img: np.ndarray, sigma: float) -> np.ndarray:
    """三次盒式模糊近似高斯（纯 numpy），代替 OpenCV 以减小打包体积。
    大半径先降采样再模糊、最后放大：只用来填透明区的颜色，精度足够，速度快很多。"""
    # 小半径（紧贴身体边缘）不降采样，保证边缘颜色准确
    f = 8 if sigma >= 12 else 4 if sigma >= 4 else 1
    x = img.astype(np.float32)
    if f > 1:
        h, w = x.shape[:2]
        x = x[:h - h % f, :w - w % f]
        x = x.reshape(h // f, f, w // f, f, *x.shape[2:]).mean((1, 3))
        sigma /= f
    r = max(1, int(round((np.sqrt(4 * sigma * sigma + 1) - 1) / 2)))
    for _ in range(3):
        x = _box(_box(x, r, 0), r, 1)
    if f > 1:
        # 双线性放大回原尺寸（最近邻放大会留下 f×f 的方块，看起来是锯齿）
        x = _upsample(x, f, h, 0)
        x = _upsample(x, f, w, 1)
    return x


def _upsample(x: np.ndarray, f: int, n: int, axis: int) -> np.ndarray:
    """沿一个轴把降采样 f 倍的数据线性插值回长度 n（按像素中心对齐）。"""
    m = x.shape[axis]
    u = np.clip((np.arange(n) + 0.5) / f - 0.5, 0, m - 1)
    i0 = np.floor(u).astype(int)
    i1 = np.minimum(i0 + 1, m - 1)
    shape = [1] * x.ndim
    shape[axis] = n
    t = (u - i0).astype(np.float32).reshape(shape)
    return np.take(x, i0, axis=axis) * (1 - t) + np.take(x, i1, axis=axis) * t


def clean_alpha(alpha: np.ndarray, lo: int = 12, hi: int = 245) -> np.ndarray:
    """低于 lo 归 0、高于 hi 归 255，中间线性拉伸：消除背景灰雾和身体发虚。"""
    a = (alpha.astype(np.float32) - lo) * (255.0 / (hi - lo))
    return np.clip(a + 0.5, 0, 255).astype(np.uint8)


def bleed_colors(rgb: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    """透明度 < 200 的像素颜色换成附近身体颜色（模糊加权平均，半径 2/6/18 逐级填充）。
    防止 H.264 的 4:2:0 色度压缩和画面缩放把透明区的颜色混进边缘，形成描边。"""
    m = (alpha >= 200).astype(np.float32)
    if m.min() == 1 or m.max() == 0:
        return rgb
    # 颜色和权重拼成 4 通道一起模糊，少做一轮
    src = np.concatenate([rgb.astype(np.float32) * m[..., None], m[..., None]], -1)
    filled = src[..., :3].copy()
    done = m > 0
    # 半径 2 只影响身体外 ~6px 的一圈：裁出包围这圈的矩形再模糊，避开大片空白
    ys, xs = np.nonzero(m)
    pad = 12
    y0, y1 = max(ys.min() - pad, 0), min(ys.max() + pad + 1, m.shape[0])
    x0, x1 = max(xs.min() - pad, 0), min(xs.max() + pad + 1, m.shape[1])
    for sigma in (2, 6, 18):
        if sigma == 2:
            b = np.zeros_like(src)
            b[y0:y1, x0:x1] = _blur(src[y0:y1, x0:x1], sigma)
        else:
            b = _blur(src, sigma)
        w = b[..., 3]
        c = b[..., :3] / np.maximum(w, 1e-4)[..., None]
        need = ~done & (w > 1e-3)
        filled[need] = c[need]
        done |= need
    return np.clip(filled + 0.5, 0, 255).astype(np.uint8)


def downsample(rgb: np.ndarray, alpha: np.ndarray):
    """SS×SS 块平均缩小。颜色按透明度加权（预乘），透明区的颜色不会混进边缘。"""
    a = alpha.astype(np.float32)
    pm = rgb.astype(np.float32) * a[..., None]
    a_sum = a.reshape(H, SS, W, SS).sum((1, 3))
    c_sum = pm.reshape(H, SS, W, SS, 3).sum((1, 3))
    out_rgb = np.clip(c_sum / np.maximum(a_sum, 1e-3)[..., None] + 0.5, 0, 255).astype(np.uint8)
    return out_rgb, (a_sum / (SS * SS) + 0.5).astype(np.uint8)


def process(frame: np.ndarray, solid: int, clear: int):
    """frame 是 SW×SH 的高分辨率帧，返回 W×H 的颜色和透明度。"""
    rgb, alpha = key(frame, solid, clear)
    rgb, alpha = downsample(rgb, clean_alpha(alpha))
    return bleed_colors(rgb, alpha), alpha


def stack(rgb: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    return np.concatenate([rgb, np.repeat(alpha[..., None], 3, -1)], 0)


def read_frame(src: Path, t: float) -> np.ndarray:
    """取 t 秒处的一帧，缩放到 SW×SH（超采样分辨率）RGB。"""
    cmd = [FFMPEG, "-v", "error", "-ss", str(t), "-i", str(src), "-frames:v", "1",
           "-vf", SCALE_VF, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    data = subprocess.run(cmd, capture_output=True, check=True).stdout
    if len(data) < FRAME_BYTES:
        raise ValueError("读取帧失败")
    return np.frombuffer(data[:FRAME_BYTES], np.uint8).reshape(SH, SW, 3)


def rgba_png(rgb: np.ndarray, alpha: np.ndarray) -> bytes:
    """用 ffmpeg 把 RGBA 帧编码成 PNG（不依赖 Pillow）。"""
    raw = np.concatenate([rgb, alpha[..., None]], -1).tobytes()
    cmd = [FFMPEG, "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{W}x{H}", "-i", "-",
           "-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "-"]
    return subprocess.run(cmd, input=raw, capture_output=True, check=True).stdout


def preview_png(src: Path, t: float, solid: int, clear: int) -> bytes:
    """抠图后的单帧 PNG（带 alpha），页面叠棋盘格预览；和导出走同一套处理。"""
    rgb, alpha = process(read_frame(src, t), solid, clear)
    return rgba_png(rgb, alpha)


def stacked_png(src: Path, t: float, solid: int, clear: int) -> bytes:
    """堆叠后的原始帧（上彩色下灰度），给页面看素材实际长什么样。"""
    rgb, alpha = process(read_frame(src, t), solid, clear)
    st = stack(rgb, alpha)
    full = np.full((H * 2, W, 1), 255, np.uint8)
    cmd = [FFMPEG, "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{W}x{H * 2}", "-i", "-",
           "-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "-"]
    raw = np.concatenate([st, full], -1).tobytes()
    return subprocess.run(cmd, input=raw, capture_output=True, check=True).stdout


def export(src: Path, dst: Path, solid: int, clear: int, crf: int = 20,
           duration: float = 0, on_progress=None, on_proc=None) -> int:
    """逐帧解码 → 抠像 → 去噪 → 颜色外扩 → 上下拼接 → H.264 编码。返回帧数。
    on_progress(0~1) 回报进度；on_proc(进程列表) 便于外部在退出时结束子进程。"""
    tmp = dst.with_name(dst.name + ".part.mp4")
    if not src.is_file():
        raise FileNotFoundError(f"找不到输入文件: {src}")
    # 解码端的报错写到临时文件：用管道的话 stderr 写满会卡住解码
    dec_log = dst.with_name(dst.name + ".dec.log")
    dec_err = dec_log.open("wb")
    dec = subprocess.Popen([FFMPEG, "-v", "error", "-i", str(src), "-vf", SCALE_VF,
                            "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                           stdout=subprocess.PIPE, stderr=dec_err)
    enc = subprocess.Popen([
        FFMPEG, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H * 2}",
        "-r", str(FPS), "-i", "-",
        "-vf", "scale=out_color_matrix=bt709:out_range=tv:flags=accurate_rnd+full_chroma_int,format=yuv420p",
        "-an", "-c:v", "libx264", "-preset", "slow", "-crf", str(crf), "-profile:v", "high",
        "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
        "-movflags", "+faststart", str(tmp)],
        stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    if on_proc:
        on_proc([dec, enc])
    total = max(duration * FPS, 1)
    n = 0
    try:
        while True:
            buf = dec.stdout.read(FRAME_BYTES)
            if len(buf) < FRAME_BYTES:
                break
            rgb, alpha = process(np.frombuffer(buf, np.uint8).reshape(SH, SW, 3), solid, clear)
            enc.stdin.write(stack(rgb, alpha).tobytes())
            n += 1
            if on_progress and n % 6 == 0:
                on_progress(min(n / total, 0.999))
    finally:
        enc.stdin.close()
    err = enc.stderr.read().decode("utf-8", "replace")
    failed = dec.wait() or enc.wait() or n == 0
    dec_err.close()
    err = (dec_log.read_text("utf-8", "replace") + err).strip()
    dec_log.unlink(missing_ok=True)
    if failed:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(err or "没有读到任何视频帧")
    tmp.replace(dst)
    return n


def main() -> None:
    p = argparse.ArgumentParser(description="绿幕视频转堆叠 Alpha MP4（手机网页透明播放用）")
    p.add_argument("input", type=Path)
    p.add_argument("output", type=Path)
    p.add_argument("--solid", type=int, default=40, help="绿色程度不超过它的像素完全不透明；人物被抠掉时调大")
    p.add_argument("--clear", type=int, default=110, help="绿色程度不低于它的像素完全透明；背景残留绿色时调小")
    p.add_argument("--crf", type=int, default=20, help="画质，越小越清晰")
    a = p.parse_args()
    if a.solid >= a.clear:
        raise SystemExit("--solid 必须小于 --clear")
    a.output.parent.mkdir(parents=True, exist_ok=True)
    n = export(a.input, a.output, a.solid, a.clear, a.crf)
    print(f"完成: {a.output}  {n} 帧  {a.output.stat().st_size / 1048576:.1f} MB")


if __name__ == "__main__":
    main()
