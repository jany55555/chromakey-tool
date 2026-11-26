"""绿幕抠图工具（本地服务，仅监听 127.0.0.1）。

用法: python app.py   浏览器会自动打开 http://127.0.0.1:8790（被占用则顺延）
上传的视频和导出结果放在系统临时目录，程序退出时删除。
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from chromakey import (FFMPEG, FFPROBE, VIDEO_EXTS, build_filter,
                       detect_key_color, encode_args)
import stacked

# PyInstaller 打包后静态资源解压在 sys._MEIPASS
RES = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
WORK = Path(tempfile.gettempdir()) / f"chromakey-tool-{uuid.uuid4().hex[:8]}"
PAGE = RES / "static" / "index.html"
PORT = 8790
MAX_UPLOAD = 4 << 30  # 4GB
PREVIEW_MAX_W = 960
ID_RE = re.compile(r"^[0-9a-f]{32}$")
COLOR_RE = re.compile(r"^0x[0-9A-Fa-f]{6}$")
JOBS: dict[str, dict] = {}

def probe(path: Path) -> dict:
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,r_frame_rate:format=duration", "-of", "json", str(path)],
        capture_output=True, check=True, text=True).stdout
    j = json.loads(out)
    s = j["streams"][0]
    num, den = (s.get("r_frame_rate") or "0/1").split("/")
    return {"width": s["width"], "height": s["height"],
            "fps": round(int(num) / max(int(den), 1), 3),
            "duration": float(j["format"].get("duration") or 0)}


def src_path(vid: str) -> Path:
    if not ID_RE.match(vid):
        raise ValueError("非法 id")
    hits = [p for p in WORK.glob(f"{vid}.*") if p.suffix.lower() in VIDEO_EXTS]
    if not hits:
        raise FileNotFoundError("视频不存在")
    return hits[0]

def parse_params(q: dict) -> dict:
    """校验前端参数，非法值直接抛 ValueError。"""
    def num(key, default, lo, hi, cast=float):
        v = cast(q.get(key, default))
        if not lo <= v <= hi:
            raise ValueError(f"{key} 超出范围 {lo}~{hi}")
        return v
    color = str(q.get("color", "auto"))
    if color != "auto" and not COLOR_RE.match(color):
        raise ValueError("color 格式应为 0xRRGGBB")
    fmt = str(q.get("format", "webm"))
    if fmt not in ("webm", "mov"):
        raise ValueError("format 只能是 webm 或 mov")
    width = num("width", 0, 0, 7680, int)
    return {"color": color, "format": fmt,
            "similarity": num("similarity", 0.15, 0.01, 1),
            "blend": num("blend", 0.05, 0, 1),
            "despill": num("despill", 0.5, 0, 1),
            "crf": num("crf", 30, 0, 63, int),
            "width": width or None, "time": num("time", 0, 0, 86400)}

def parse_stacked(q: dict) -> dict:
    """堆叠 MP4 页签的参数：solid < clear，都是「绿色程度」阈值。"""
    def num(key, default, lo, hi, cast=float):
        v = cast(q.get(key, default))
        if not lo <= v <= hi:
            raise ValueError(f"{key} 超出范围 {lo}~{hi}")
        return v
    p = {"solid": num("solid", 40, 0, 254, int), "clear": num("clear", 110, 1, 255, int),
         "crf": num("crf", 20, 10, 40, int), "time": num("time", 0, 0, 86400)}
    if p["solid"] >= p["clear"]:
        raise ValueError("不透明阈值必须小于透明阈值")
    return p


def run_stacked_export(job_id: str, src: Path, p: dict, name: str) -> None:
    job = JOBS[job_id]
    try:
        dst = WORK / f"{job_id}.mp4"
        frames = stacked.export(
            src, dst, p["solid"], p["clear"], p["crf"], job["duration"],
            on_progress=lambda v: job.__setitem__("progress", v),
            on_proc=lambda procs: job.__setitem__("procs", procs))
        job.update(status="done", progress=1.0, file=dst.name, frames=frames,
                   size=dst.stat().st_size, download_name=f"{Path(name).stem}.mp4")
    except Exception as e:  # noqa: BLE001 - 错误回传给页面
        job.update(status="error", error=str(e))
    finally:
        job.pop("procs", None)


def resolve_color(src: Path, p: dict) -> str:
    return detect_key_color(src) if p["color"] == "auto" else p["color"].upper()


def preview_png(src: Path, p: dict) -> bytes:
    """抠图后的单帧 PNG（带 alpha），供页面叠棋盘格预览。"""
    vf = build_filter(resolve_color(src, p), p["similarity"], p["blend"],
                      p["despill"], None)
    vf += f",scale='min({PREVIEW_MAX_W},iw)':-2,format=rgba"
    cmd = [FFMPEG, "-v", "error", "-ss", str(p["time"]), "-i", str(src),
           "-frames:v", "1", "-vf", vf, "-f", "image2pipe", "-c:v", "png", "-"]
    return subprocess.run(cmd, capture_output=True, check=True).stdout


def original_png(src: Path, t: float) -> bytes:
    cmd = [FFMPEG, "-v", "error", "-ss", str(t), "-i", str(src), "-frames:v", "1",
           "-vf", f"scale='min({PREVIEW_MAX_W},iw)':-2",
           "-f", "image2pipe", "-c:v", "png", "-"]
    return subprocess.run(cmd, capture_output=True, check=True).stdout

def run_export(job_id: str, src: Path, p: dict, name: str) -> None:
    job = JOBS[job_id]
    try:
        color = resolve_color(src, p)
        vf = build_filter(color, p["similarity"], p["blend"], p["despill"], p["width"])
        dst = WORK / f"{job_id}.{p['format']}"
        cmd = [FFMPEG, "-y", "-v", "error", "-nostats", "-progress", "pipe:1",
               "-i", str(src), *encode_args(p["format"], vf, p["crf"], True), str(dst)]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace")
        job["proc"] = proc
        dur = max(job["duration"], 0.001)
        for line in proc.stdout:
            if line.startswith("out_time_us="):
                us = line.split("=", 1)[1].strip()
                if us.isdigit():
                    job["progress"] = min(int(us) / 1e6 / dur, 0.999)
        err = proc.stderr.read()
        if proc.wait() != 0:
            raise RuntimeError(err.strip() or "ffmpeg 失败")
        job.update(status="done", progress=1.0, file=dst.name, color=color,
                   size=dst.stat().st_size,
                   download_name=f"{Path(name).stem}.{p['format']}")
    except Exception as e:  # noqa: BLE001 - 错误回传给页面
        job.update(status="error", error=str(e))
    finally:
        job.pop("proc", None)

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 安静一点
        pass

    def handle_one_request(self):
        # 浏览器取消旧的预览请求很常见，连接断开不当作错误
        try:
            super().handle_one_request()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            self.close_connection = True

    def send_bytes(self, code: int, body: bytes, ctype: str, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj, code=200):
        self.send_bytes(code, json.dumps(obj, ensure_ascii=False).encode(),
                        "application/json; charset=utf-8")

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/":
                self.send_bytes(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif u.path == "/api/preview":
                src = src_path(q.get("id", ""))
                self.send_bytes(200, preview_png(src, parse_params(q)), "image/png")
            elif u.path == "/api/original":
                src = src_path(q.get("id", ""))
                t = parse_params({"time": q.get("time", 0)})["time"]
                self.send_bytes(200, original_png(src, t), "image/png")
            elif u.path == "/api/stacked/preview":
                src = src_path(q.get("id", ""))
                p = parse_stacked(q)
                fn = stacked.stacked_png if q.get("view") == "raw" else stacked.preview_png
                self.send_bytes(200, fn(src, p["time"], p["solid"], p["clear"]), "image/png")
            elif u.path == "/api/job":
                job = JOBS.get(q.get("id", ""))
                if not job:
                    return self.send_json({"error": "任务不存在"}, 404)
                self.send_json({k: v for k, v in job.items() if k not in ("proc", "procs")})
            elif u.path == "/download":
                self.download(q.get("id", ""))
            elif u.path == "/api/stacked/file":
                self.serve_video(q.get("id", ""))
            else:
                self.send_json({"error": "not found"}, 404)
        except (ValueError, FileNotFoundError) as e:
            self.send_json({"error": str(e)}, 400)
        except subprocess.CalledProcessError as e:
            msg = (e.stderr or b"").decode("utf-8", "replace").strip() or "ffmpeg 失败"
            self.send_json({"error": msg}, 500)

    def download(self, job_id: str):
        job = JOBS.get(job_id)
        if not job or job.get("status") != "done":
            return self.send_json({"error": "文件未就绪"}, 404)
        path = WORK / job["file"]
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header("Content-Disposition",
                         f"attachment; filename*=UTF-8''{quote(job['download_name'])}")
        self.end_headers()
        with path.open("rb") as f:
            shutil.copyfileobj(f, self.wfile, 1 << 20)

    def serve_video(self, job_id: str):
        """导出的 MP4 按 video/mp4 输出并支持 Range，页面里的播放检查用（循环、拖动需要分段读取）。"""
        job = JOBS.get(job_id)
        if not job or job.get("status") != "done" or not job["file"].endswith(".mp4"):
            return self.send_json({"error": "文件未就绪"}, 404)
        path = WORK / job["file"]
        size = path.stat().st_size
        m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range") or "")
        start, end = 0, size - 1
        if m and (m[1] or m[2]):
            start = int(m[1]) if m[1] else max(size - int(m[2]), 0)
            end = min(int(m[2]), size - 1) if m[1] and m[2] else size - 1
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                return self.end_headers()
        self.send_response(206 if m and (m[1] or m[2]) else 200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if m and (m[1] or m[2]):
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with path.open("rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(left, 1 << 20))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def do_POST(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/api/upload":
                self.upload(q)
            elif u.path == "/api/export":
                src = src_path(q.get("id", ""))
                p = parse_params(q)
                job_id = uuid.uuid4().hex
                JOBS[job_id] = {"status": "running", "progress": 0.0,
                                "duration": probe(src)["duration"]}
                threading.Thread(target=run_export, daemon=True,
                                 args=(job_id, src, p, q.get("name", "output"))).start()
                self.send_json({"job": job_id})
            elif u.path == "/api/stacked/export":
                src = src_path(q.get("id", ""))
                p = parse_stacked(q)
                job_id = uuid.uuid4().hex
                JOBS[job_id] = {"status": "running", "progress": 0.0,
                                "duration": probe(src)["duration"]}
                threading.Thread(target=run_stacked_export, daemon=True,
                                 args=(job_id, src, p, q.get("name", "output"))).start()
                self.send_json({"job": job_id})
            else:
                self.send_json({"error": "not found"}, 404)
        except (ValueError, FileNotFoundError) as e:
            self.send_json({"error": str(e)}, 400)

    def upload(self, q: dict):
        """请求体就是原始文件字节，文件名放在 ?name= 里。"""
        name = unquote(q.get("name", "video.mp4"))
        ext = Path(name).suffix.lower()
        if ext not in VIDEO_EXTS:
            raise ValueError(f"不支持的格式 {ext}")
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_UPLOAD:
            raise ValueError("文件为空或超过 4GB")
        vid = uuid.uuid4().hex
        dst = WORK / f"{vid}{ext}"
        remaining = length
        with dst.open("wb") as f:
            while remaining:
                chunk = self.rfile.read(min(remaining, 1 << 20))
                if not chunk:
                    break
                f.write(chunk)
                remaining -= len(chunk)
        if remaining:
            dst.unlink(missing_ok=True)
            raise ValueError("上传中断")
        try:
            info = probe(dst)
            color = detect_key_color(dst)
        except (subprocess.CalledProcessError, KeyError, IndexError):
            dst.unlink(missing_ok=True)
            raise ValueError("无法识别的视频文件") from None
        self.send_json({"id": vid, "name": name, "color": color, **info})

def cleanup_stale() -> None:
    """清掉上次异常退出（比如直接关窗口）留下的临时目录。"""
    for d in Path(tempfile.gettempdir()).glob("chromakey-tool-*"):
        if d != WORK:
            shutil.rmtree(d, ignore_errors=True)


def main() -> None:
    cleanup_stale()
    WORK.mkdir(exist_ok=True)
    # Windows 上 SO_REUSEADDR 会允许和其他程序抢同一个端口，必须关掉
    ThreadingHTTPServer.allow_reuse_address = False
    for port in range(PORT, PORT + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    else:
        raise SystemExit(f"端口 {PORT}~{PORT + 19} 都被占用")
    url = f"http://127.0.0.1:{port}"
    print("=" * 50)
    print(f" 绿幕抠图工具已启动: {url}")
    print(" 浏览器没自动打开的话，复制上面的地址打开即可")
    print(" 用完直接关闭这个窗口")
    print("=" * 50)
    threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for job in JOBS.values():
            for proc in [job.get("proc"), *(job.get("procs") or [])]:
                if proc:
                    proc.kill()
        server.server_close()
        shutil.rmtree(WORK, ignore_errors=True)


if __name__ == "__main__":
    main()
