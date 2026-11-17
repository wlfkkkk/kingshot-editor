"""Kingshot Editor - a tiny video editor for YouTube Shorts (1080x1920).

Cut parts out, blur areas, add music, export. Preview uses Qt Multimedia,
export uses FFmpeg (handles AV1, HEVC / H.265, HDR iPhone clips, etc.).
"""
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QPointF, QRect, QRectF, QSettings, QSize, QStandardPaths, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (QBrush, QColor, QDesktopServices, QIcon, QImage, QKeySequence,
                           QPainter, QPalette, QPen, QShortcut, QTransform)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoFrame, QVideoFrameFormat, QVideoSink

try:
    import numpy as np
except ImportError:  # only needed for 10-bit preview without a hardware decoder
    np = None
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout,
                               QLineEdit,
                               QLabel, QMainWindow, QMenu, QMessageBox, QProgressBar, QPushButton, QScrollArea,
                               QScrollBar, QSizePolicy, QSlider, QToolBar, QVBoxLayout, QWidget)

APP_NAME = "Kingshot Editor"
UPDATE_REPO = "wlfkkkk/kingshot-editor"   # public GitHub repo: latest.json + kingshot_editor.py on main = the update
APP_VERSION = "1.0.1"     # bump on every release: shown in the ? window, used by the installer
FORMATS = {   # key: (width, height, label)
    "short": (1080, 1920, "YouTube Short · 9:16 (max 3 min)"),
    "tall": (1080, 1920, "YouTube video · 9:16 vertical"),
    "long": (1920, 1080, "YouTube video · 16:9 wide"),
}
SOURCE_FORMAT = "source"             # export at exactly the video's own size (phone recordings that aren't 9:16)
SOURCE_LABEL = "Same size as the video · no resizing"
SHORTS_MAX = 180                     # seconds
APP_DIR = Path(__file__).resolve().parent
NO_WINDOW = 0x08000000 if os.name == "nt" else 0   # don't flash a console for ffmpeg

# ---------------------------------------------------------------- log
LOG_DIR = APP_DIR.parent / "logs"                   # inside the install folder (%LOCALAPPDATA%\KingshotEditor\logs)
LOG_FILE = LOG_DIR / "editor-log.txt"               # what happened while editing / exporting, for troubleshooting
_log_lock = threading.Lock()


def log(msg):
    """Append one timestamped line to editor-log.txt. Never raises. Starts a fresh file past 2 MB (old one kept)."""
    try:
        with _log_lock:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > 2_000_000:
                os.replace(LOG_FILE, LOG_FILE.with_suffix(".old.txt"))
            with open(LOG_FILE, "a", encoding="utf-8", errors="replace") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg).rstrip() + "\n")
    except Exception:  # noqa: BLE001
        pass


def _log_crash(et, ev, tb):
    log("CRASH (unhandled error):\n" + "".join(traceback.format_exception(et, ev, tb)))
    sys.__excepthook__(et, ev, tb)


C_BG, C_PANEL, C_PANEL2, C_LINE = "#0f1218", "#171b24", "#1e2330", "#2a3040"
C_TEXT, C_DIM, C_ACCENT = "#e8ebf2", "#8b93a7", "#4f8cff"
C_CUT, C_BLUR, C_PH, C_MUSIC = QColor("#ff5a5f"), QColor("#a78bfa"), QColor("#ffd84a"), QColor("#34d399")


# ---------------------------------------------------------------- helpers
def clamp(v, a, b):
    return max(a, min(b, v))


def fmt(t, short=False):
    t = max(0.0, t or 0.0)
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t - h * 3600 - m * 60
    sec = f"{int(s):02d}" if short else f"{s:04.1f}"
    return f"{h}:{m:02d}:{sec}" if h else f"{m}:{sec}"


def find_tool(name):
    exe = name + (".exe" if os.name == "nt" else "")
    for d in (APP_DIR.parent / "ffmpeg", APP_DIR / "ffmpeg", APP_DIR):
        if (d / exe).exists():
            return str(d / exe)
    return shutil.which(name)


FFMPEG, FFPROBE = find_tool("ffmpeg"), find_tool("ffprobe")


def run(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          creationflags=NO_WINDOW, **kw)


def probe(path):
    """Display size (rotation applied), duration, audio, HDR."""
    r = run([FFPROBE, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)])
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "ffprobe failed")
    j = json.loads(r.stdout)
    info = {"duration": float(j.get("format", {}).get("duration") or 0), "has_audio": False, "w": 0, "h": 0,
            "hdr": False, "codec": "", "vbr": 0, "abr": 0}
    fmt_br = int(j.get("format", {}).get("bit_rate") or 0)
    for s in j.get("streams", []):
        if s.get("codec_type") == "video" and not info["w"] and s.get("disposition", {}).get("attached_pic") != 1:
            w, h = int(s.get("width") or 0), int(s.get("height") or 0)
            rot = 0
            for sd in s.get("side_data_list", []) or []:
                if "rotation" in sd:
                    rot = int(float(sd["rotation"]))
            rot = rot or int(float(s.get("tags", {}).get("rotate", 0) or 0))
            if abs(rot) % 180 == 90:
                w, h = h, w
            try:
                num, den = (s.get("avg_frame_rate") or s.get("r_frame_rate") or "30/1").split("/")
                fps = float(num) / float(den) if float(den) else 30.0
            except ValueError:
                fps = 30.0
            info.update(w=w, h=h, rot=rot, codec=s.get("codec_name", ""), fps=fps if 1 < fps < 241 else 30.0,
                        hdr=s.get("color_transfer") in ("smpte2084", "arib-std-b67"))
            if not info["duration"] and s.get("duration"):
                info["duration"] = float(s["duration"])
            info["vbr"] = int(s.get("bit_rate") or 0)
        elif s.get("codec_type") == "audio":
            info["has_audio"] = True
            info["abr"] = info["abr"] or int(s.get("bit_rate") or 0)
    if not info["vbr"] and fmt_br:       # MKV/WebM often only list the total bitrate
        info["vbr"] = max(0, fmt_br - info["abr"])
    return info


_ZSCALE = None


def has_zscale():
    global _ZSCALE
    if _ZSCALE is None:
        r = run([FFMPEG, "-hide_banner", "-filters"])
        _ZSCALE = " zscale " in r.stdout
    return _ZSCALE


def place(vw, vh, mode, zoom, pos, W, H):
    """Where the video goes inside the WxH output frame: (x, y, w, h)."""
    if mode == "fill":
        s = max(W / vw, H / vh) * zoom
        dw, dh = vw * s, vh * s
        return (W - dw) * pos, (H - dh) * pos, dw, dh
    s = min(W / vw, H / vh) * zoom
    dw, dh = vw * s, vh * s
    x = (W - dw) * pos if dw > W else (W - dw) / 2
    y = (H - dh) * pos if dh > H else (H - dh) / 2
    return x, y, dw, dh


def blur_sigma(strength):          # in output (1080 wide) pixels
    return strength * 3.2


def frame_to_image(frame):
    """QVideoFrame -> QImage. Qt can't convert 10-bit (HDR/HEVC Main10) frames on the CPU, so do it here."""
    img = frame.toImage()
    if not img.isNull() or np is None:
        return img
    PF = QVideoFrameFormat.PixelFormat
    fmt = frame.surfaceFormat().pixelFormat()
    if fmt not in (PF.Format_YUV420P10, PF.Format_P010, PF.Format_P016):
        return img
    if not frame.map(QVideoFrame.MapMode.ReadOnly):
        return img
    try:
        w, h = frame.width() // 2 * 2, frame.height() // 2 * 2

        def plane(i, rows):
            bpl = frame.bytesPerLine(i)
            return np.frombuffer(frame.bits(i), dtype=np.uint16, count=bpl * rows // 2).reshape(rows, bpl // 2)
        if fmt == PF.Format_YUV420P10:          # 10 bits in the low bits, 3 planes
            y = plane(0, h)[:, :w] >> 2
            u = plane(1, h // 2)[:, :w // 2] >> 2
            v = plane(2, h // 2)[:, :w // 2] >> 2
        else:                                   # P010: high bits, interleaved UV
            y = plane(0, h)[:, :w] >> 8
            uv = plane(1, h // 2)[:, :w]
            u, v = uv[:, 0::2] >> 8, uv[:, 1::2] >> 8
        u = u.repeat(2, 0).repeat(2, 1)
        v = v.repeat(2, 0).repeat(2, 1)
        Y = (y.astype(np.float32) - 16) * 1.164
        U, V = u.astype(np.float32) - 128, v.astype(np.float32) - 128
        rgb = np.dstack((Y + 1.793 * V, Y - 0.213 * U - 0.533 * V, Y + 2.112 * U))
        rgb = np.ascontiguousarray(np.clip(rgb, 0, 255).astype(np.uint8))
        hh, ww = rgb.shape[:2]
        return QImage(rgb.data, ww, hh, ww * 3, QImage.Format_RGB888).copy()
    except Exception:  # noqa: BLE001
        return img
    finally:
        frame.unmap()


class ElidedLabel(QLabel):
    """One-line label that shortens long text with '…' instead of widening the side panel
    (song file names are long and have no spaces to wrap at)."""

    def __init__(self, text=""):
        super().__init__()
        self._full = text
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setToolTip(text)
        self._elide()

    def _elide(self):
        w = max(20, self.width())
        self.setText(self.fontMetrics().elidedText(self._full, Qt.ElideMiddle, w))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._elide()

    def minimumSizeHint(self):
        return QSize(20, super().minimumSizeHint().height())


# ---------------------------------------------------------------- project model
class Project:
    def __init__(self):
        self.src = None
        self.info = {}
        self.dur = 0.0
        self.cuts = []      # {id,a,b}
        self.blurs = []     # {id,a,b,x,y,w,h,s}  box in 0..1 of the output frame
        self.sel = None     # (a,b)
        self.active = None  # ("cut"|"blur", id)
        self.strength = 6
        self.frame, self.pos, self.zoom = "blurbg", 0.5, 1.0
        self.format = "short"
        self.vvol = 1.0
        self.speed = 1.0           # video speed (picture + its own sound)
        self.mspeed = 1.0          # music speed (all songs), independent of the video speed
        self.codec = "auto"        # export codec: auto (AV1, then HEVC, then H.264) | av1 | hevc | h264
        self.quality = "same"      # same (match the original's bitrate / sharpness) | small
        self.vsound = "fast"       # video's own sound when sped up: fast (stays in sync, default) | normal | mute
        # music clips live on the FINAL timeline (after cuts), so songs keep playing across cuts
        self.musics = []    # {id,path,name,sdur,start,inn,len,vol,fade,rep}
        self.preview = None  # small stand-in file for the preview window (AV1 that Windows can't play)
        self.yt_files = []  # songs the app downloaded from YouTube for this project (cleaned up after export)
        self.exported = False
        self._id = 1

    def _even(self, v):
        return max(2, int(v) // 2 * 2)       # video encoders need even sizes

    @property
    def scale(self):
        """'Same as original': keep 1440p / 4K sources at their sharpness instead of shrinking to 1080p."""
        if self.quality != "same" or not self.info.get("w"):
            return 1.0
        short = min(self.info["w"], self.info["h"])
        return clamp(short / 1080, 1.0, 2.0)

    @property
    def W(self):
        if self.format == SOURCE_FORMAT and self.info.get("w"):
            return self._even(self.info["w"])
        return int(round(FORMATS[self.format if self.format in FORMATS else "short"][0] * self.scale / 2)) * 2

    @property
    def H(self):
        if self.format == SOURCE_FORMAT and self.info.get("h"):
            return self._even(self.info["h"])
        return int(round(FORMATS[self.format if self.format in FORMATS else "short"][1] * self.scale / 2)) * 2

    def target_bitrate(self):
        """Video bitrate to export with in 'same' mode: the original's, adjusted if the picture size changes."""
        if self.quality != "same" or not self.info.get("vbr"):
            return None
        src_px = self.info["w"] * self.info["h"]
        out_px = self.W * self.H
        return int(self.info["vbr"] * clamp(out_px / max(1, src_px), 0.5, 2.0))

    def new_id(self):
        self._id += 1
        return self._id

    def norm_cuts(self):
        cs = sorted(({**c, "a": clamp(c["a"], 0, self.dur), "b": clamp(c["b"], 0, self.dur)} for c in self.cuts),
                    key=lambda c: c["a"])
        out = []
        for c in cs:
            if c["b"] - c["a"] <= 0.02:
                continue
            if out and c["a"] <= out[-1]["b"] + 0.01:
                out[-1]["b"] = max(out[-1]["b"], c["b"])
            else:
                out.append(c)
        self.cuts = out
        if self.active and not self.item(*self.active):
            self.active = None

    def item(self, kind, id_):
        for it in {"cut": self.cuts, "blur": self.blurs, "music": self.musics}[kind]:
            if it["id"] == id_:
                return it
        return None

    def active_item(self):
        return self.item(*self.active) if self.active else None

    def cut_at(self, t):
        for c in self.cuts:
            if c["a"] <= t < c["b"]:
                return c
        return None

    def to_out(self, t):
        o = t
        for c in self.cuts:
            if c["b"] <= t:
                o -= c["b"] - c["a"]
            elif c["a"] < t:
                o -= t - c["a"]
        return o

    def out_dur(self):
        return self.to_out(self.dur)

    def keep_segments(self):
        segs, p = [], 0.0
        for c in self.cuts:
            if c["a"] > p + 0.02:
                segs.append((p, c["a"]))
            p = max(p, c["b"])
        if self.dur > p + 0.02:
            segs.append((p, self.dur))
        return segs

    def from_out(self, o):
        """Final-video time -> source time."""
        acc = 0.0
        for a, b in self.keep_segments():
            if o <= acc + (b - a) + 1e-9:
                return a + max(0.0, o - acc)
            acc += b - a
        return self.dur

    # the FINAL timeline = after cuts and after speed-up. Music lives on it.
    def fin(self, t):
        return self.to_out(t) / self.speed

    def from_fin(self, f):
        return self.from_out(f * self.speed)

    def fin_dur(self):
        return self.out_dur() / self.speed

    def music_span(self, m):
        return self.from_fin(m["start"]), self.from_fin(m["start"] + m["len"])

    def music_end(self, m):
        return min(m["start"] + m["len"], self.fin_dur())

    @staticmethod
    def clip_pos(m, o, ms=1.0):
        rel = o - m["start"]
        if rel < 0 or rel >= m["len"]:
            return None
        pos = m["inn"] + rel * ms
        if pos >= m["sdur"] - 0.05:
            return pos % m["sdur"] if m["rep"] else None
        return pos

    def clip_gain(self, m, o):
        g = m["vol"]
        if m["fade"]:
            rel, ln = o - m["start"], self.music_end(m) - m["start"]
            g *= clamp(rel / 1.0, 0, 1) * clamp((ln - rel) / 2.5, 0, 1)
        return g

    def snapshot(self):
        return copy.deepcopy((self.cuts, self.blurs, self.musics, self.mspeed))

    def restore(self, snap):
        self.cuts, self.blurs, self.musics, self.mspeed = copy.deepcopy(snap)
        if self.active and not self.active_item():
            self.active = None


# ---------------------------------------------------------------- ffmpeg command
_ENC = {}          # codec -> working graphics-card encoders
_ENC_ERR = {}
CPU_ENC = {"hevc": "libx265", "h264": "libx264"}
CODEC_LABEL = {"av1": "AV1", "hevc": "HEVC", "h264": "H.264"}
_ENC_LIST = None


def have_encoder(name):
    global _ENC_LIST
    if _ENC_LIST is None:
        _ENC_LIST = run([FFMPEG, "-hide_banner", "-encoders"]).stdout
    return f" {name} " in _ENC_LIST


def cpu_encoders(codec):
    """Processor encoders for a codec, best first. AV1 uses SVT-AV1 (fast), else libaom."""
    if codec == "av1":
        return [e for e in ("libsvtav1", "libaom-av1") if have_encoder(e)]
    return [CPU_ENC[codec]]


def enc_codec(enc):
    """Which codec an encoder name produces."""
    if "av1" in enc:
        return "av1"
    return "hevc" if enc.startswith("hevc") or enc == "libx265" else "h264"


def export_plan(codec):
    """Encoders to try, in order. 'auto' (Recommended) = graphics card: AV1, HEVC, H.264; then the processor:
    AV1, HEVC, H.264."""
    if codec == "auto":
        return (hw_encoders("av1") + hw_encoders("hevc") + hw_encoders("h264")
                + cpu_encoders("av1") + [CPU_ENC["hevc"], CPU_ENC["h264"]])
    return hw_encoders(codec) + cpu_encoders(codec)


def enc_name(enc):
    kind = {"nvenc": "NVIDIA graphics card (NVENC)", "amf": "AMD graphics card (AMF)",
            "qsv": "Intel graphics (Quick Sync)", "mf": "graphics card (Windows Media Foundation)"}
    if enc.startswith("lib"):
        return f"processor ({enc[3:]})"
    return kind.get(enc.split("_", 1)[1], enc)


def hw_encoders(codec="hevc"):
    """Graphics-card encoders for this codec that actually work on this PC (tested once per run)."""
    if codec not in _ENC:
        found = _ENC[codec] = []
        cands = [f"{codec}_nvenc", f"{codec}_amf", f"{codec}_qsv"] + ([f"{codec}_mf"] if os.name == "nt" else [])
        have = run([FFMPEG, "-hide_banner", "-encoders"]).stdout
        for enc in cands:
            if f" {enc} " not in have:
                _ENC_ERR[enc] = "not included in this FFmpeg build"
                continue
            extra = ["-hw_encoding", "1"] if enc.endswith("_mf") else []
            try:
                r = run([FFMPEG, "-hide_banner", "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=1280x720:r=30:d=1",
                         "-pix_fmt", "nv12", "-c:v", enc, *extra, "-f", "null", "-"], timeout=40)
                if r.returncode == 0:
                    found.append(enc)
                    _ENC_ERR.pop(enc, None)
                else:
                    junk = ("Nothing was written", "Conversion failed", "Error while", "Error sending", "Task finished")
                    lines = [x.strip() for x in r.stderr.splitlines() if x.strip() and not any(j in x for j in junk)]
                    _ENC_ERR[enc] = (" / ".join(lines[:2]) or "failed")[:300]
            except Exception as ex:  # noqa: BLE001
                _ENC_ERR[enc] = str(ex)[:300]
        log = APP_DIR.parent / "encoder-check.txt"
        try:
            log.write_text("Working: " + (", ".join(e for v in _ENC.values() for e in v) or "none") + "\n\n" +
                           "\n".join(f"{k}: {v}" for k, v in _ENC_ERR.items()), encoding="utf-8")
        except OSError:
            pass
    return _ENC[codec]


def encoder_args(enc, total, W, H, rate=None):
    """H.264 or HEVC. HEVC is tagged hvc1 so Windows, Apple devices and YouTube all read it.
    rate (bits/s) = match the original video's bitrate; None = quality-based (smaller files)."""
    big = W * H >= 1920 * 1080
    av1 = enc_codec(enc) == "av1"
    hevc = enc_codec(enc) == "hevc"
    tag = ["-tag:v", "hvc1"] if hevc else []
    prof = [] if av1 else ["-profile:v", "main" if hevc else "high"]
    if av1:
        return av1_args(enc, total, rate, big)
    nv = "p5"                                   # NVENC quality preset (p4 is faster but compresses slightly worse)
    if rate:
        r = ["-b:v", str(rate), "-maxrate", str(int(rate * 1.5)), "-bufsize", str(rate * 2)]
        if enc.endswith("_nvenc"):
            return ["-c:v", enc, "-preset", nv, "-rc", "vbr", *r, *prof] + tag
        if enc.endswith("_qsv"):
            return ["-c:v", enc, "-preset", "medium", *r, *prof] + tag
        if enc.endswith("_amf"):
            return ["-c:v", enc, "-quality", "balanced", "-rc", "vbr_peak", *r] + tag
        if enc.endswith("_mf"):
            return ["-c:v", enc, "-hw_encoding", "1", "-b:v", str(rate)] + tag
        preset = ("fast" if hevc else "veryfast") if total > 900 else "medium"
        if hevc:
            return ["-c:v", "libx265", "-preset", preset, *r, "-x265-params", "log-level=error"] + tag
        return ["-c:v", "libx264", "-preset", preset, *r, "-profile:v", "high"]
    q = 23 if hevc else 21
    if enc.endswith("_nvenc"):
        return ["-c:v", enc, "-preset", nv, "-rc", "vbr", "-cq", str(q), "-b:v", "0", *prof] + tag
    if enc.endswith("_qsv"):
        return ["-c:v", enc, "-preset", "medium", "-global_quality", str(q), *prof] + tag
    if enc.endswith("_amf"):
        return ["-c:v", enc, "-quality", "balanced", "-rc", "cqp", "-qp_i", str(q - 1), "-qp_p", str(q + 1)] + tag
    if enc.endswith("_mf"):
        rate = ("10M" if big else "6M") if hevc else ("12M" if big else "8M")
        return ["-c:v", enc, "-hw_encoding", "1", "-b:v", rate] + tag
    long_ = total > 900       # CPU: good quality for short clips, faster preset for long recordings
    if hevc:
        preset, crf = ("fast", "24") if long_ else ("medium", "22")
        return ["-c:v", "libx265", "-preset", preset, "-crf", crf, "-x265-params", "log-level=error"] + tag
    preset, crf = ("veryfast", "20") if long_ else ("medium", "18")
    return ["-c:v", "libx264", "-preset", preset, "-crf", crf, "-profile:v", "high"]


def av1_args(enc, total, rate, big):
    """'Same quality as the original' keeps the original's bitrate (no quality loss, similar file size), the
    same as the HEVC / H.264 paths; AV1 at that bitrate looks at least as good."""
    long_ = total > 900
    r = ["-b:v", str(rate), "-maxrate", str(int(rate * 1.5)), "-bufsize", str(rate * 2)] if rate else []
    q = 30
    if enc.endswith("_nvenc"):
        return ["-c:v", enc, "-preset", "p5", "-rc", "vbr", *(r or ["-cq", str(q), "-b:v", "0"])]
    if enc.endswith("_qsv"):
        return ["-c:v", enc, "-preset", "medium", *(r or ["-global_quality", str(q)])]
    if enc.endswith("_amf"):
        return ["-c:v", enc, "-quality", "balanced", *(["-rc", "vbr_peak"] + r if r else
                                                        ["-rc", "cqp", "-qp_i", "110", "-qp_p", "115"])]
    if enc == "libsvtav1":
        return ["-c:v", enc, "-preset", "10" if long_ else "8", *(r[:2] if r else ["-crf", str(q + 2)])]
    return ["-c:v", "libaom-av1", "-cpu-used", "8" if long_ else "6", "-row-mt", "1", "-threads", "0",
            *(r[:2] if r else ["-crf", str(q + 2), "-b:v", "0"])]


def uses_hwdec(p):
    """AV1 sources are heavy to decode in software, so the export asks the graphics card to decode them
    (falls back to the processor by itself, and the export dialog retries without it if FFmpeg gives up)."""
    # AV1 is left to FFmpeg's software decoder (libdav1d, multi-threaded): 2.6.2, because the first AV1 sources
    # exported at 2.5x real time on the test PC (HEVC/H.264 sources: ~11-15x) with either encoder.
    return p.info.get("codec") in ("hevc", "h264") and len(p.keep_segments()) <= 3


NV_DEC = {"av1": "av1_cuvid", "hevc": "hevc_cuvid", "h264": "h264_cuvid"}     # NVIDIA video decoders
_NV = None
_DEC_LIST = None
_FILT_LIST = None


def nvidia_present():
    """True when a working NVIDIA encoder exists, i.e. an NVIDIA graphics card."""
    global _NV
    if _NV is None:
        _NV = any(e.endswith("_nvenc") for c in ("av1", "hevc", "h264") for e in hw_encoders(c))
    return _NV


def have_decoder(name):
    global _DEC_LIST
    if _DEC_LIST is None:
        _DEC_LIST = run([FFMPEG, "-hide_banner", "-decoders"]).stdout
    return f" {name} " in _DEC_LIST


def have_filter(name):
    global _FILT_LIST
    if _FILT_LIST is None:
        _FILT_LIST = run([FFMPEG, "-hide_banner", "-filters"]).stdout
    return f" {name} " in _FILT_LIST


def export_geometry(p):
    """Where the source lands in the output frame: scale size, crop and pad offsets (all even numbers)."""
    W, H = p.W, p.H
    ev = lambda v: int(round(v / 2)) * 2
    x, y, dw, dh = place(p.info["w"], p.info["h"], p.frame, p.zoom, p.pos, W, H)
    sw, sh = max(2, ev(dw)), max(2, ev(dh))
    x, y = ev(x), ev(y)
    cx, cy = max(0, -x), max(0, -y)
    px, py = max(0, x), max(0, y)
    cw, ch = ev(min(sw - cx, W - px)), ev(min(sh - cy, H - py))
    return sw, sh, cx, cy, px, py, cw, ch


def decode_plan(p, enc):
    """Decoding modes to try for this export, fastest first; the export dialog steps down when FFmpeg fails.
    gpu   = NVIDIA decoder, frames stay on the graphics card through resize and speed change into NVENC
    cuvid = NVIDIA decoder, the remaining filters run on the processor
    auto  = FFmpeg's own hardware decoding (H.264 / HEVC only)
    cpu   = processor decoding"""
    info = p.info
    codec, W, H = info.get("codec"), p.W, p.H
    modes = []
    if (codec in NV_DEC and len(p.keep_segments()) <= 3 and not info.get("hdr")
            and nvidia_present() and have_decoder(NV_DEC[codec])):
        sw, sh, cx, cy, px, py, cw, ch = export_geometry(p)
        if (enc.endswith("_nvenc") and len(p.keep_segments()) == 1 and not p.blurs and p.frame != "blurbg"
                and (cw, ch, cx, cy) == (sw, sh, 0, 0) and (px, py, cw, ch) == (0, 0, W, H)
                and have_filter("scale_cuda")):
            modes.append("gpu")
        modes.append("cuvid")
    if uses_hwdec(p):
        modes.append("auto")
    modes.append("cpu")
    return modes


def build_export(p: Project, out_path, enc="libx265", dec="cpu"):  # noqa: C901
    """FFmpeg command. Each kept part is opened with a fast seek (-ss), so cut-out hours are never decoded.
    dec is one of decode_plan()'s modes: gpu, cuvid, auto, cpu."""
    info = p.info
    vw, vh = info["w"], info["h"]
    W, H = p.W, p.H
    tw, th = (108, 192) if H > W else (192, 108)
    ev = lambda v: int(round(v / 2)) * 2
    sw, sh, cx, cy, px, py, cw, ch = export_geometry(p)

    segs = p.keep_segments()
    total = sum(b - a for a, b in segs)
    has_a = info.get("has_audio")
    args = [FFMPEG, "-y", "-hide_banner"]
    nvdec = NV_DEC.get(info.get("codec"), "")
    hw = {"gpu": ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda", "-c:v", nvdec],
          "cuvid": ["-c:v", nvdec], "auto": ["-hwaccel", "auto"]}.get(dec, [])
    g = []
    if dec == "gpu" and len(segs) == 1:
        a0, b0 = segs[0]
        cut = [] if (a0 < 0.01 and b0 >= p.dur - 0.01) else ["-ss", f"{a0:.3f}", "-t", f"{b0 - a0:.3f}"]
        args += hw + cut + ["-i", str(p.src)]
        n_in = 1
        g.append("[0:v]setpts=PTS-STARTPTS[vc]")
        if has_a:
            g.append("[0:a]asetpts=PTS-STARTPTS,aresample=48000[ac]")
    elif len(segs) == 1 and segs[0][0] < 0.01 and segs[0][1] >= p.dur - 0.01:
        args += hw + ["-i", str(p.src)]
        n_in = 1
        g.append("[0:v]setpts=PTS-STARTPTS[vc]")
        if has_a:
            g.append("[0:a]asetpts=PTS-STARTPTS[ac]")
    else:
        n_in = len(segs)
        for i, (a, b) in enumerate(segs):
            args += hw + ["-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", str(p.src)]
        ins = ""
        for i in range(len(segs)):
            g.append(f"[{i}:v]setpts=PTS-STARTPTS[sv{i}]")
            ins += f"[sv{i}]"
            if has_a:
                g.append(f"[{i}:a]asetpts=PTS-STARTPTS,aresample=48000[sa{i}]")
                ins += f"[sa{i}]"
        g.append(f"{ins}concat=n={len(segs)}:v=1:a={1 if has_a else 0}[vc]" + ("[ac]" if has_a else ""))

    pre = ""
    if info.get("hdr") and has_zscale():   # iPhone HDR clips: tone-map to normal colours
        pre += ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,"
                "zscale=t=bt709:m=bt709:r=tv,")
    pre += "format=yuv420p,"
    # skip filters that would change nothing (a 1080x1920 recording exported as 1080x1920): less work per frame
    fg = (f"scale={sw}:{sh}:flags=bicubic," if (sw, sh) != (vw, vh) else "")
    fg += (f"crop={cw}:{ch}:{cx}:{cy}," if (cw, ch, cx, cy) != (sw, sh, 0, 0) else "") + "setsar=1"
    no_pad = (px, py, cw, ch) == (0, 0, W, H)
    if dec == "gpu":     # frames stay on the graphics card: resize there (bicubic, as on the processor), no copy to RAM
        gs = f"scale_cuda={sw}:{sh}:interp_algo=bicubic," if (sw, sh) != (vw, vh) else ""
        g.append(f"[vc]{gs}setsar=1[v0]")
    elif p.frame == "blurbg":
        g.append(f"[vc]{pre}split=2[s1][s2]")
        g.append(f"[s1]{fg}[fg]")
        g.append(f"[s2]scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th},boxblur=4:1,"
                 f"scale={W}:{H}:flags=bicubic,lutyuv=y=val*0.65,setsar=1[bg]")
        g.append(f"[bg][fg]overlay={px}:{py}[v0]")
    else:
        g.append(f"[vc]{pre}{fg}{'' if no_pad else f',pad={W}:{H}:{px}:{py}:black'}[v0]")
    cur = "v0"
    for i, b in enumerate(p.blurs):
        oa, ob = p.to_out(b["a"]), p.to_out(b["b"])       # blur times on the final timeline
        if ob - oa < 0.01:
            continue
        bx, by = ev(b["x"] * W), ev(b["y"] * H)
        bw, bh = ev(min(b["w"] * W, W - bx)), ev(min(b["h"] * H, H - by))
        if bw < 4 or bh < 4:
            continue
        g.append(f"[{cur}]split=2[m{i}][c{i}]")
        g.append(f"[c{i}]crop={bw}:{bh}:{bx}:{by},gblur=sigma={blur_sigma(b['s']) * min(W, H) / 1080:.1f}:steps=2[b{i}]")
        g.append(f"[m{i}][b{i}]overlay={bx}:{by}:enable='between(t,{oa:.3f},{ob:.3f})'[w{i}]")
        cur = f"w{i}"
    sp = p.speed
    if sp != 1:     # speed-up: faster timestamps, keep a sane frame rate
        g.append(f"[{cur}]setpts=PTS/{sp},fps={min(60.0, info.get('fps', 30) * sp):.3f}[vs]")
        cur = "vs"
    if dec == "gpu":      # 8-bit 4:2:0 (nv12) on the card, which is what NVENC takes directly
        g.append(f"[{cur}]scale_cuda=format=nv12[vout]")
    else:
        g.append(f"[{cur}]format={'nv12' if enc.endswith(('_qsv', '_mf')) else 'yuv420p'}[vout]")
    total = total / sp      # length of the final video

    if has_a and not (sp != 1 and p.vsound == "mute"):
        tempo = f",atempo={sp}" if sp != 1 and p.vsound == "fast" else ""
        g.append(f"[ac]aresample=48000:async=1{tempo},atrim=end={total:.3f},volume={p.vvol:.2f}[va]")
    else:
        if has_a:
            g.append("[ac]anullsink")      # video sound muted
        g.append(f"anullsrc=r=48000:cl=stereo,atrim=end={total:.3f}[va]")
    aout = "va"
    mix = ["[va]"]
    for m in p.musics:
        ln = min(m["len"], total - m["start"])
        if ln < 0.05:
            continue
        if m["rep"]:
            args += ["-stream_loop", "-1"]
        if m["inn"] > 0:
            args += ["-ss", f"{m['inn']:.3f}"]
        args += ["-i", str(m["path"])]
        k = len(mix)
        mt = f"atempo={p.mspeed:g}," if p.mspeed != 1 else ""      # music speed, pitch kept
        f = (f"[{n_in}:a]aresample=48000,aformat=channel_layouts=stereo,{mt}atrim=end={ln:.3f},asetpts=PTS-STARTPTS,"
             f"volume={m['vol']:.2f}")
        if m["fade"]:
            f += ",afade=t=in:d=1"
            if ln > 3:
                f += f",afade=t=out:st={ln - 2.5:.3f}:d=2.5"
        ms = int(round(m["start"] * 1000))
        if ms > 0:
            f += f",adelay=delays={ms}:all=1"
        g.append(f + f"[mu{k}]")
        mix.append(f"[mu{k}]")
        n_in += 1
    if len(mix) > 1:
        g.append(f"{''.join(mix)}amix=inputs={len(mix)}:duration=first:normalize=0[am]")
        aout = "am"
    args += ["-filter_complex", ";".join(g), "-map", "[vout]", "-map", f"[{aout}]",
             *encoder_args(enc, total, W, H, p.target_bitrate()), "-max_muxing_queue_size", "4096",
             "-c:a", "aac", "-b:a", f"{clamp(info.get('abr') or 192000, 128000, 320000) // 1000}k", "-ar", "48000", "-movflags", "+faststart",
             "-t", f"{total:.3f}", "-progress", "pipe:1", "-nostats", str(out_path)]
    return args, total


# ---------------------------------------------------------------- video view
class VideoView(QWidget):
    """Shows the 9:16 output preview and lets you draw / move blur boxes."""

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.img = None
        self.setMinimumSize(240, 300)
        self.setMouseTracking(True)
        self._drag = None
        self.setFocusPolicy(Qt.NoFocus)

    @property
    def p(self):
        return self.win.p

    def out_rect(self):
        m = 12
        aw, ah = self.width() - 2 * m, self.height() - 2 * m
        W, H = self.p.W, self.p.H
        s = max(0.01, min(aw / W, ah / H))
        w, h = W * s, H * s
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def compose(self, r, t):
        dpr = self.devicePixelRatioF()                  # crisp on 125% / 150% Windows scaling
        cw, ch = max(1, int(r.width() * dpr)), max(1, int(r.height() * dpr))
        comp = QImage(cw, ch, QImage.Format_RGB32)
        comp.fill(QColor("black"))
        img = self.img
        if img is None or img.isNull():
            return comp
        p = self.p
        q = QPainter(comp)
        q.setRenderHint(QPainter.SmoothPixmapTransform)
        vw, vh = img.width(), img.height()
        if p.frame == "blurbg":
            tw, th = (27, 48) if p.H > p.W else (48, 27)
            tiny = img.scaled(tw, th, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            tiny = tiny.copy((tiny.width() - tw) // 2, (tiny.height() - th) // 2, tw, th)
            q.drawImage(QRectF(0, 0, cw, ch), tiny)
            q.fillRect(0, 0, cw, ch, QColor(0, 0, 0, 95))
        x, y, dw, dh = place(vw, vh, p.frame, p.zoom, p.pos, cw, ch)
        if dw < vw * 0.98:      # shrinking: proper area filtering instead of bilinear (no shimmer, sharper)
            q.drawImage(QPointF(x, y), img.scaled(max(1, round(dw)), max(1, round(dh)),
                                                  Qt.IgnoreAspectRatio, Qt.SmoothTransformation))
        else:
            q.drawImage(QRectF(x, y, dw, dh), img)
        sc = min(cw, ch) / 1080
        for b in p.blurs:
            if not (b["a"] <= t < b["b"]):
                continue
            br = QRect(int(b["x"] * cw), int(b["y"] * ch), max(1, int(b["w"] * cw)), max(1, int(b["h"] * ch)))
            br = br.intersected(comp.rect())
            if br.width() < 2 or br.height() < 2:
                continue
            piece = comp.copy(br)
            k = max(2.0, blur_sigma(b["s"]) * sc * 1.3)
            small = piece.scaled(max(1, int(br.width() / k)), max(1, int(br.height() / k)),
                                 Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            small = small.scaled(max(1, int(br.width() / k * 2)), max(1, int(br.height() / k * 2)),
                                 Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            q.drawImage(QRectF(br), small)
        q.end()
        return comp

    def paintEvent(self, e):
        q = QPainter(self)
        q.fillRect(self.rect(), QColor(C_BG))
        p = self.p
        if not p.src:
            q.setPen(QColor(C_DIM))
            f = q.font(); f.setPointSize(13); q.setFont(f)
            q.drawText(self.rect(), Qt.AlignCenter,
                       "🎬\n\nOpen a video\n\nClick “Open video” or drag a video file onto this window.\n"
                       "MP4 / MOV / MKV / WebM (H.264, HEVC or AV1), from a few seconds up to many hours.\n"
                       "Export as a YouTube Short (1080×1920) or a normal YouTube video (1920×1080).")
            return
        r = self.out_rect()
        t = self.win.cur_time()
        q.drawImage(r, self.compose(r, t))
        q.setRenderHint(QPainter.Antialiasing)
        act = p.active[1] if p.active and p.active[0] == "blur" else None
        for b in (p.blurs if self.win.blur_tools else []):
            if not (b["a"] <= t < b["b"]):
                continue
            br = self.box_rect(b, r)
            if b["id"] == act:
                q.setPen(QPen(C_BLUR, 2))
                q.setBrush(QColor(167, 139, 250, 35))
                q.drawRect(br)
                q.setPen(QPen(C_BLUR, 2)); q.setBrush(QColor("white"))
                for c in self.corners(br).values():
                    q.drawRoundedRect(QRectF(c.x() - 6, c.y() - 6, 12, 12), 3, 3)
            else:
                q.setPen(QPen(QColor(255, 255, 255, 210), 2, Qt.DashLine))
                q.setBrush(Qt.NoBrush)
                q.drawRect(br)

    def box_rect(self, b, r=None):
        r = r or self.out_rect()
        return QRectF(r.left() + b["x"] * r.width(), r.top() + b["y"] * r.height(), b["w"] * r.width(),
                      b["h"] * r.height())

    @staticmethod
    def corners(br):
        return {"tl": br.topLeft(), "tr": br.topRight(), "bl": br.bottomLeft(), "br": br.bottomRight()}

    def norm(self, pos):
        r = self.out_rect()
        return clamp((pos.x() - r.left()) / r.width(), 0, 1), clamp((pos.y() - r.top()) / r.height(), 0, 1)

    def hit(self, pos):
        if not self.win.blur_tools:
            return None, None
        t = self.win.cur_time()
        p = self.p
        act = p.active_item() if p.active and p.active[0] == "blur" else None
        if act and act["a"] <= t < act["b"]:
            for k, c in self.corners(self.box_rect(act)).items():
                if abs(c.x() - pos.x()) <= 9 and abs(c.y() - pos.y()) <= 9:
                    return act, k
        for b in reversed(p.blurs):
            if b["a"] <= t < b["b"] and self.box_rect(b).contains(pos):
                return b, "move"
        return None, None

    def mouseMoveEvent(self, e):
        pos = e.position()
        if not self._drag:
            b, h = self.hit(pos) if self.p.src else (None, None)
            self.setCursor({"tl": Qt.SizeFDiagCursor, "br": Qt.SizeFDiagCursor, "tr": Qt.SizeBDiagCursor,
                            "bl": Qt.SizeBDiagCursor, "move": Qt.SizeAllCursor}.get(
                h, Qt.CrossCursor if self.win.blur_tools else Qt.ArrowCursor))
            return
        d = self._drag
        nx, ny = self.norm(pos)
        dx, dy = nx - d["x0"], ny - d["y0"]
        if not d["moved"]:
            if abs(dx) + abs(dy) < 0.004:
                return
            d["moved"] = True
            self.win.push_undo()
            if d["mode"] == "new":
                rng = self.p.sel or (0, self.p.dur)
                b = {"id": self.p.new_id(), "a": rng[0], "b": rng[1], "x": d["x0"], "y": d["y0"], "w": 0, "h": 0,
                     "s": self.p.strength}
                self.p.blurs.append(b)
                self.p.active = ("blur", b["id"])
                d["b"] = b
        b, o = d["b"], d["o"]
        if d["mode"] == "new":
            b["x"], b["y"] = min(d["x0"], nx), min(d["y0"], ny)
            b["w"], b["h"] = abs(nx - d["x0"]), abs(ny - d["y0"])
        elif d["mode"] == "move":
            b["x"] = clamp(o["x"] + dx, 0, 1 - o["w"])
            b["y"] = clamp(o["y"] + dy, 0, 1 - o["h"])
        else:
            l, t, r_, bt = o["x"], o["y"], o["x"] + o["w"], o["y"] + o["h"]
            h = d["mode"]
            if h[1] == "l": l += dx
            else: r_ += dx
            if h[0] == "t": t += dy
            else: bt += dy
            l, r_ = sorted((clamp(l, 0, 1), clamp(r_, 0, 1)))
            t, bt = sorted((clamp(t, 0, 1), clamp(bt, 0, 1)))
            b.update(x=l, y=t, w=max(0.01, r_ - l), h=max(0.01, bt - t))
        self.update()

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton or not self.p.src:
            return
        self.win.pause()
        if not self.win.blur_tools:      # blur drawing is off until 'Blur tools' is ticked
            return
        nx, ny = self.norm(e.position())
        b, h = self.hit(e.position())
        if b:
            self.p.active = ("blur", b["id"])
            self._drag = {"mode": h, "b": b, "o": dict(b), "x0": nx, "y0": ny, "moved": False}
            self.win.changed()
        else:
            self._drag = {"mode": "new", "b": None, "o": None, "x0": nx, "y0": ny, "moved": False}

    def mouseReleaseEvent(self, e):
        d, self._drag = self._drag, None
        if not d:
            return
        p = self.p
        if d["mode"] == "new":
            if not d["moved"]:
                if p.active:
                    p.active = None
                    self.win.changed()
                return
            b = d["b"]
            if b["w"] < 0.015 or b["h"] < 0.008:
                p.blurs.remove(b)
                self.win.undo_stack.pop()
                p.active = None
                self.win.changed()
                return
            whole = b["a"] <= 0 and b["b"] >= p.dur
            p.sel = None
            self.win.changed()
            self.win.toast("Blur added for the whole video. Drag the ends of its purple bar to change when it shows."
                           if whole else f"Blur added from {fmt(b['a'])} to {fmt(b['b'])}")
        elif d["moved"]:
            self.win.changed()


# ---------------------------------------------------------------- timeline
class Timeline(QWidget):
    LBL, RULER, TRACK, GAP = 58, 20, 34, 6

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.setMouseTracking(True)
        self.relayout()
        self._drag = None
        self.setFocusPolicy(Qt.NoFocus)
        self.v0, self.vlen = 0.0, 0.0     # visible window (seconds); vlen 0 = whole video
        self.on_view = None

    @property
    def p(self):
        return self.win.p

    def relayout(self):
        """The Blur lane only exists while 'Blur tools' is ticked."""
        n = 3 if getattr(self.win, "blur_tools", False) else 2
        self.setFixedHeight(self.RULER + n * (self.TRACK + self.GAP) + 4)

    def area(self):
        return self.LBL, self.width() - self.LBL - 6

    def view(self):
        d = self.p.dur
        if not d:
            return 0.0, 1.0
        ln = d if not self.vlen or self.vlen >= d else self.vlen
        return clamp(self.v0, 0, d - ln), ln

    def set_view(self, v0, vlen):
        d = self.p.dur
        if not d:
            return
        vlen = clamp(vlen, min(d, 2.0), d)
        self.vlen = 0.0 if vlen >= d - 1e-6 else vlen
        self.v0 = clamp(v0, 0, d - vlen)
        self.update()
        if self.on_view:
            self.on_view()

    def zoom_by(self, f, at_t=None):
        v0, ln = self.view()
        at = (v0 + ln / 2) if at_t is None else at_t
        nl = ln / f
        self.set_view(at - (at - v0) * nl / ln, nl)

    def follow(self, t):
        v0, ln = self.view()
        if self.vlen and not (v0 <= t <= v0 + ln):
            self.set_view(t - ln * 0.1, ln)

    def wheelEvent(self, e):
        if not self.p.dur:
            return
        dy = e.angleDelta().y() or e.angleDelta().x()
        v0, ln = self.view()
        if e.modifiers() & Qt.ShiftModifier or e.angleDelta().x():
            self.set_view(v0 - dy / 120 * ln * 0.15, ln)            # pan
        else:
            self.zoom_by(1.25 if dy > 0 else 0.8, self.t_of(e.position().x()))   # zoom at mouse

    def x_of(self, t):
        x0, w = self.area()
        v0, ln = self.view()
        return x0 + ((t - v0) / ln) * w if self.p.dur else x0

    def t_of(self, x):
        x0, w = self.area()
        v0, ln = self.view()
        return clamp(v0 + (x - x0) / max(1, w) * ln, 0, self.p.dur)

    def lane_rect(self, lane):
        x0, w = self.area()
        y = self.RULER + 2 + lane * (self.TRACK + self.GAP)
        return QRectF(x0, y, w, self.TRACK)

    def item_rect(self, kind, it):
        lr = self.lane_rect({"cut": 0, "music": 1, "blur": 2}[kind])
        a, b = self.p.music_span(it) if kind == "music" else (it["a"], it["b"])
        x1, x2 = self.x_of(a), self.x_of(b)
        pad = 0 if kind == "cut" else 3
        return QRectF(x1, lr.top() + pad, max(4, x2 - x1), lr.height() - 2 * pad)

    def paintEvent(self, e):
        q = QPainter(self)
        q.setRenderHint(QPainter.Antialiasing)
        q.fillRect(self.rect(), QColor(C_PANEL))
        p = self.p
        q.setPen(QColor(C_DIM))
        f = q.font(); f.setPointSize(9); q.setFont(f)
        for lane, name in ((0, "Video"), (1, "Music"), (2, "Blur"))[:3 if self.win.blur_tools else 2]:
            lr = self.lane_rect(lane)
            q.drawText(QRectF(6, lr.top(), self.LBL - 8, lr.height()), Qt.AlignVCenter, name)
            q.setPen(Qt.NoPen)
            q.setBrush(QColor("#2a3b60") if lane == 0 and p.dur else QColor(C_PANEL2))
            q.drawRoundedRect(lr, 6, 6)
            q.setPen(QColor(C_DIM))
        if not p.dur:
            return
        x0, w = self.area()
        v0, ln = self.view()
        q.setClipRect(QRectF(x0 - 1, 0, w + 8, self.height()))
        steps = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800]
        step = next((s for s in steps if s / ln * w >= 72), 21600)
        f.setPointSize(8); q.setFont(f)
        t = (v0 // step) * step
        while t <= min(p.dur, v0 + ln) + 1e-6:
            x = self.x_of(t)
            q.setPen(QColor("#3d4560")); q.drawLine(int(x), self.RULER - 5, int(x), self.RULER)
            q.setPen(QColor(C_DIM)); q.drawText(QRectF(x - 30, 0, 60, self.RULER - 5), Qt.AlignCenter, fmt(t, step >= 1))
            t += step
        act = p.active or (None, None)
        for kind, items, col in (("cut", p.cuts, C_CUT), ("blur", p.blurs, C_BLUR), ("music", p.musics, C_MUSIC)):
            for it in (items if kind != "blur" or self.win.blur_tools else []):
                r = self.item_rect(kind, it)
                if kind == "cut":
                    q.setPen(Qt.NoPen)
                    q.setBrush(QBrush(QColor(255, 90, 95, 220), Qt.BDiagPattern))
                    q.fillRect(r, QColor(120, 25, 35, 230))
                    q.drawRect(r)
                elif kind == "blur":
                    q.setPen(QPen(col, 1)); q.setBrush(QColor(167, 139, 250, 140)); q.drawRoundedRect(r, 5, 5)
                else:
                    q.setPen(QPen(col, 1)); q.setBrush(QColor(52, 211, 153, 110)); q.drawRoundedRect(r, 5, 5)
                if r.width() > 60:
                    q.setPen(QColor("white"))
                    label = {"cut": "✂ cut", "blur": "▦ blur"}.get(kind) or "♪ " + it["name"]
                    q.drawText(r.adjusted(12, 0, -12, 0), Qt.AlignCenter,
                               q.fontMetrics().elidedText(label, Qt.ElideRight, int(r.width() - 24)))
                q.setPen(QPen(QColor(255, 255, 255, 190), 3, Qt.SolidLine, Qt.RoundCap))
                for ex in (r.left() + 4, r.right() - 4):
                    q.drawLine(QPointF(ex, r.top() + r.height() * .3), QPointF(ex, r.bottom() - r.height() * .3))
                if act == (kind, it["id"]):
                    q.setPen(QPen(QColor("white"), 2)); q.setBrush(Qt.NoBrush)
                    q.drawRoundedRect(r.adjusted(-1, -1, 1, 1), 5, 5)
        ta, tb = self.trim_edges()
        lr0 = self.lane_rect(0)
        for xe, side in ((self.x_of(ta), 1), (self.x_of(tb), -1)):
            hr = QRectF(xe if side > 0 else xe - 10, lr0.top(), 10, lr0.height())
            q.setPen(Qt.NoPen); q.setBrush(QColor("#e8ebf2")); q.drawRoundedRect(hr, 3, 3)
            q.setPen(QPen(QColor(40, 40, 40), 2))
            q.drawLine(QPointF(hr.center().x(), hr.top() + 9), QPointF(hr.center().x(), hr.bottom() - 9))
        if p.sel:
            lr = self.lane_rect(0)
            x1, x2 = self.x_of(p.sel[0]), self.x_of(p.sel[1])
            q.setPen(QPen(QColor(C_ACCENT), 2)); q.setBrush(QColor(79, 140, 255, 70))
            q.drawRoundedRect(QRectF(x1, lr.top(), max(2, x2 - x1), lr.height()), 5, 5)
        x = self.x_of(self.win.cur_time())
        q.setPen(QPen(C_PH, 2)); q.drawLine(QPointF(x, 2), QPointF(x, self.height() - 4))
        q.setBrush(C_PH); q.setPen(Qt.NoPen)
        q.drawPolygon([QPointF(x - 6, 0), QPointF(x + 6, 0), QPointF(x, 8)])

    def trim_edges(self):
        p = self.p
        a = next((c["b"] for c in p.cuts if c["a"] <= 0.001), 0.0)
        b = next((c["a"] for c in p.cuts if c["b"] >= p.dur - 0.001), p.dur)
        return a, b

    def trim_at(self, pos):
        """Grabbing the start / end of the video bar trims the video."""
        if not self.lane_rect(0).adjusted(0, -2, 0, 2).contains(pos):
            return None
        a, b = self.trim_edges()
        xa, xb = self.x_of(a), self.x_of(b)
        if abs(pos.x() - xa) <= 9 and (pos.x() >= xa - 2 or a > 0):
            return "a"
        if abs(pos.x() - xb) <= 9:
            return "b"
        return None

    def set_trim(self, which, t):
        p = self.p
        a, b = self.trim_edges()
        if which == "a":
            t = clamp(t, 0, b - 0.1)
            c = next((c for c in p.cuts if c["a"] <= 0.001), None)
            if t < 0.01:
                if c:
                    p.cuts.remove(c)
            elif c:
                c["b"] = t
            else:
                p.cuts.append({"id": p.new_id(), "a": 0.0, "b": t})
        else:
            t = clamp(t, a + 0.1, p.dur)
            c = next((c for c in p.cuts if c["b"] >= p.dur - 0.001), None)
            if t > p.dur - 0.01:
                if c:
                    p.cuts.remove(c)
            elif c:
                c["a"] = t
            else:
                p.cuts.append({"id": p.new_id(), "a": t, "b": p.dur})
        return t

    def item_at(self, pos):
        for kind, items in (("cut", self.p.cuts), ("blur", self.p.blurs), ("music", self.p.musics)):
            if kind == "blur" and not self.win.blur_tools:
                continue
            for it in reversed(items):
                r = self.item_rect(kind, it)
                if r.adjusted(-3, 0, 3, 0).contains(pos):
                    if pos.x() <= r.left() + 8:
                        return kind, it, "a"
                    if pos.x() >= r.right() - 8:
                        return kind, it, "b"
                    return kind, it, "move"
        return None, None, None

    def mouseMoveEvent(self, e):
        pos = e.position()
        d = self._drag
        p = self.p
        if not d:
            _, _, mode = self.item_at(pos) if p.dur else (None, None, None)
            if p.dur and self.trim_at(pos):
                mode = "a"
            self.setCursor(Qt.SizeHorCursor if mode in ("a", "b") else Qt.OpenHandCursor if mode else
                           Qt.PointingHandCursor if pos.y() < self.RULER else Qt.IBeamCursor)
            return
        t = self.t_of(pos.x())
        if d["kind"] == "scrub":
            self.win.seek(t)
        elif d["kind"] == "trim":
            if not d["moved"]:
                if abs(pos.x() - d["x0"]) < 3:
                    return
                d["moved"] = True
                self.win.push_undo()
            tt = self.set_trim(d["which"], t)
            self.win.seek(tt if d["which"] == "a" else max(0, tt - 0.04))
            self.win.changed(light=True)
        elif d["kind"] == "sel":
            if not d["moved"] and abs(pos.x() - d["x0"]) < 4:
                return
            d["moved"] = True
            p.sel = (min(d["t0"], t), max(d["t0"], t))
            p.active = None
            self.win.seek(t)
            self.win.changed(light=True)
        else:
            it, mode = d["it"], d["mode"]
            if not d["moved"]:
                if abs(pos.x() - d["x0"]) < 3:
                    return
                d["moved"] = True
                self.win.push_undo()
            if d["ikind"] == "music":
                do = p.fin(t) - d["o0"]
                if mode == "move":
                    it["start"] = max(0.0, d["s0"] + do)
                elif mode == "a":
                    lo = 0.0 if it["rep"] else max(0.0, d["s0"] - d["i0"] / p.mspeed)
                    ns = clamp(d["s0"] + do, lo, d["s0"] + d["l0"] - 0.5)
                    it["start"], it["len"] = ns, d["l0"] - (ns - d["s0"])
                    it["inn"] = (d["i0"] + (ns - d["s0"]) * p.mspeed) % it["sdur"]
                else:
                    hi = 1e9 if it["rep"] else (it["sdur"] - it["inn"]) / p.mspeed
                    it["len"] = clamp(d["l0"] + do, 0.5, hi)
                self.win.changed(light=True)
                return
            dt = t - d["t0"]
            if mode == "a":
                it["a"] = clamp(d["a0"] + dt, 0, it["b"] - 0.05)
                self.win.seek(it["a"])
            elif mode == "b":
                it["b"] = clamp(d["b0"] + dt, it["a"] + 0.05, p.dur)
                self.win.seek(max(it["a"], it["b"] - 0.04))
            else:
                ln = d["b0"] - d["a0"]
                it["a"] = clamp(d["a0"] + dt, 0, p.dur - ln)
                it["b"] = it["a"] + ln
                self.win.seek(it["a"])
            self.win.changed(light=True)

    def mousePressEvent(self, e):
        p = self.p
        if e.button() != Qt.LeftButton or not p.dur:
            return
        self.win.pause()
        pos = e.position()
        t = self.t_of(pos.x())
        if pos.y() < self.RULER:
            self._drag = {"kind": "scrub"}
            self.win.seek(t)
            return
        which = self.trim_at(pos)
        if which:
            p.active = None
            self._drag = {"kind": "trim", "which": which, "x0": pos.x(), "moved": False}
            self.win.changed()
            return
        kind, it, mode = self.item_at(pos)
        if it:
            p.active = (kind, it["id"])
            if kind == "blur" and not (it["a"] <= self.win.cur_time() < it["b"]):
                self.win.seek(it["a"])
            self._drag = {"kind": "item", "it": it, "ikind": kind, "mode": mode, "t0": t, "x0": pos.x(),
                          "moved": False}
            if kind == "music":
                self._drag.update(o0=p.fin(t), s0=it["start"], i0=it["inn"], l0=it["len"])
            else:
                self._drag.update(a0=it["a"], b0=it["b"])
            self.win.changed()
            return
        if self.lane_rect(0).adjusted(0, -2, 0, 2).contains(pos):
            self._drag = {"kind": "sel", "t0": t, "x0": pos.x(), "moved": False}
        else:
            p.active = None
            self.win.seek(t)
            self.win.changed()

    def contextMenuEvent(self, e):
        p = self.p
        if not p.dur:
            return
        kind, it, _ = self.item_at(QPointF(e.pos()))
        if not it:
            return
        p.active = (kind, it["id"])
        self.win.changed()
        menu = QMenu(self)
        if kind == "blur":
            menu.addAction("🗑  Remove blur", lambda: self.win.remove_item("blur", it["id"]))
            menu.addAction("Make it last the whole video", lambda: self.win.blur_range(it, 0, p.dur))
        elif kind == "cut":
            menu.addAction("↩  Put this part back", lambda: self.win.remove_item("cut", it["id"]))
        else:
            menu.addAction("🗑  Remove song", lambda: self.win.remove_item("music", it["id"]))
            menu.addAction("Move it to the playhead", lambda: self.win.music_to_playhead(it))
        menu.exec(e.globalPos())

    def mouseReleaseEvent(self, e):
        d, self._drag = self._drag, None
        if not d:
            return
        p = self.p
        if d["kind"] == "sel" and not d["moved"]:
            p.sel = None
            p.active = None
            self.win.seek(d["t0"])
        if d["kind"] == "trim" and d["moved"]:
            p.norm_cuts()
            self.win.toast("Video trimmed. Drag the edge back to undo, or press Ctrl+Z.")
        if d["kind"] == "item" and d["moved"] and d["ikind"] == "cut":
            p.norm_cuts()
        self.win.changed()


# ---------------------------------------------------------------- export dialog
class Exporter(QObject):
    progress = Signal(float, float)     # fraction done, speed (x realtime)
    done = Signal(int, str)

    def __init__(self, args, total):
        super().__init__()
        self.args, self.total, self.proc, self.cancelled = args, total, None, False

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        err = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
        speed = 0.0
        t0 = time.time()
        last_log = t0
        log("ffmpeg start: " + subprocess.list2cmdline([str(a) for a in self.args]))
        try:
            self.proc = subprocess.Popen(self.args, stdout=subprocess.PIPE, stderr=err, stdin=subprocess.DEVNULL,
                                         text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
            for line in self.proc.stdout:
                k, _, v = line.strip().partition("=")
                if k == "speed":
                    try:
                        speed = float(v.rstrip("x"))
                    except ValueError:
                        pass
                elif k in ("out_time_us", "out_time_ms"):
                    try:
                        frac = clamp(int(v) / 1e6 / self.total, 0, 1)
                        self.progress.emit(frac, speed)
                        if time.time() - last_log >= 30:      # throughput trail, so a slow export can be diagnosed from the log
                            last_log = time.time()
                            log(f"export progress: {frac * 100:.1f}% at {speed:.2f}x real time after {last_log - t0:.0f}s")
                    except ValueError:
                        pass
            code = self.proc.wait()
        except Exception as ex:  # noqa: BLE001
            log(f"ffmpeg could not start: {ex}")
            self.done.emit(-1, str(ex))
            return
        err.seek(0)
        text = err.read().strip()
        tail = "\n".join(text.splitlines()[-12:])
        hints = [l.strip() for l in text.splitlines()
                 if re.search(r"hwaccel|d3d11|dxva|cuda|dav1d|libaom|Failed|fall(ing)? back|Stream mapping|-> #", l, re.I)][:10]
        if hints and code == 0:
            log("ffmpeg notes:\n" + "\n".join(hints))
        log(f"ffmpeg end: code {code}{' (cancelled)' if self.cancelled else ''} after {time.time() - t0:.1f}s"
            + (f"\n{tail}" if code != 0 else ""))
        self.done.emit(code, tail)

    def cancel(self):
        self.cancelled = True
        if self.proc and self.proc.poll() is None:
            self.proc.kill()


def project_summary(p):
    try:
        return (f"src={p.src} {p.W}x{p.H} dur={p.dur:.1f}s format={p.format} speed={p.speed:g} mspeed={p.mspeed:g} "
                f"quality={getattr(p, 'quality', '?')} cuts={[(round(c['a'], 2), round(c['b'], 2)) for c in p.cuts]} "
                f"blurs={len(p.blurs)} songs={[Path(m.get('path', '?')).name for m in p.musics]}")
    except Exception as ex:  # noqa: BLE001
        return f"(summary failed: {ex})"


def keep_awake(on):
    """Stop Windows from sleeping during a long export."""
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000001 if on else 0x80000000)
        except Exception:  # noqa: BLE001
            pass


def dur_words(sec):
    sec = int(sec)
    if sec < 90:
        return f"{max(1, sec)} s"
    if sec < 5400:
        return f"{round(sec / 60)} min"
    return f"{sec // 3600} h {sec % 3600 // 60:02d} min"


# ---------------------------------------------------------------- YouTube audio download
YT_RE = __import__("re").compile(
    r"(https?://)?(www\.|m\.|music\.)?(youtube\.com/(watch|shorts/|live/|embed/)|youtu\.be/)\S+", __import__("re").I)
YT_LOG = LOG_DIR / "yt-download-log.txt"      # last download attempt, for troubleshooting


def open_folder(path):
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


def yt_ready_dir():
    """Videos\\YT Ready. Asks Windows where Videos really is (it can be moved, e.g. into OneDrive), the same
    place setup.ps1 creates the folder."""
    base = QStandardPaths.writableLocation(QStandardPaths.MoviesLocation)
    return Path(base or (Path.home() / "Videos")) / "YT Ready"


PREVIEWS = APP_DIR.parent / "preview"                    # small H.264 stand-ins for videos Windows can't preview
DOWNLOADS = APP_DIR.parent / "downloads"                 # raw downloads + conversion work
MUSIC_DIR = Path.home() / "Music" / "Kingshot Editor"    # finished MP3s from YouTube


def clean_download_leftovers(max_age=600):
    """Raw downloaded audio/video and half-finished files in the app's downloads folder are never needed again."""
    import time
    try:
        for f in DOWNLOADS.iterdir():
            if f.is_file() and time.time() - f.stat().st_mtime > max_age:
                f.unlink(missing_ok=True)
    except OSError:
        pass


def clean_previews(keep=None):
    try:
        for f in PREVIEWS.iterdir():
            if f.is_file() and str(f) != str(keep):
                f.unlink(missing_ok=True)
    except OSError:
        pass


def proxy_command(p, out, hwdec):
    """Small 720p H.264 copy of the video, only used for the preview window (the export always reads the original)."""
    info = p.info
    vf = ""
    if info.get("hdr") and has_zscale():
        vf += ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,"
               "zscale=t=bt709:m=bt709:r=tv,")
    codec = info.get("codec")
    if (hwdec and not info.get("hdr") and codec in NV_DEC and nvidia_present() and have_decoder(NV_DEC[codec])
            and have_filter("scale_cuda") and "h264_nvenc" in hw_encoders("h264")):
        # 2.6.6: whole preview copy on the graphics card (NVIDIA decode -> resize -> NVENC). On the processor a long
        # AV1 recording took as long as the video itself (decode ~1.7x real time) before it could be edited.
        oh = min(720, info["h"]) // 2 * 2
        ow = int(round(info["w"] * oh / info["h"] / 2)) * 2
        return [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-hwaccel", "cuda", "-hwaccel_output_format",
                "cuda", "-c:v", NV_DEC[codec], "-i", str(p.src), "-map", "0:v:0", "-map", "0:a:0?",
                "-vf", f"scale_cuda={ow}:{oh}:interp_algo=bicubic,scale_cuda=format=nv12",
                "-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "27", "-b:v", "0", "-g", "15",
                "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(out)]
    vf += "scale=-2:'min(720,ih)',format=yuv420p"
    args = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error"] + (["-hwaccel", "auto"] if hwdec and codec != "av1" else [])
    return args + ["-i", str(p.src), "-map", "0:v:0", "-map", "0:a:0?", "-vf", vf,
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "27", "-g", "15", "-c:a", "aac", "-b:a", "128k",
                   "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(out)]


# ---------------------------------------------------------------- speed test (for troubleshooting slow exports)
# ---------------------------------------------------------------- updater
def _ver_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v))[:4]) or (0,)


def _http_get(url, timeout=20):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "KingshotEditor-updater", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def check_for_update():
    """Read latest.json from the repo's main branch. Returns a dict (newer version found), or None (up to date). Raises on network errors."""
    base = f"https://raw.githubusercontent.com/{UPDATE_REPO}/main/"
    info = json.loads(_http_get(base + "latest.json").decode("utf-8"))
    tag = str(info.get("version", ""))
    if _ver_tuple(tag) <= _ver_tuple(APP_VERSION):
        return None
    assets = {n: base + n for n in ("kingshot_editor.py", "kingshot_editor.py.sha256", "icon.ico")}
    if not info.get("icon"):
        assets.pop("icon.ico")
    return {"version": tag.lstrip("vV"), "assets": assets, "page": f"https://github.com/{UPDATE_REPO}",
            "notes": str(info.get("notes") or "").strip(),
            "installer_needed": bool(info.get("installer_required"))}


def apply_update(upd):
    """Download kingshot_editor.py (+ icon) from the release, check it, and swap it in. Old file kept as kingshot_editor.py.bak."""
    import hashlib
    url = upd["assets"]["kingshot_editor.py"]
    data = _http_get(url, 120)
    sums = upd["assets"].get("kingshot_editor.py.sha256")
    if sums:
        want = _http_get(sums).decode("utf-8").split()[0].lower()
        if hashlib.sha256(data).hexdigest() != want:
            raise RuntimeError("The downloaded file did not match its checksum.")
    text = data.decode("utf-8")
    m = re.search(r'^APP_VERSION = "([^"]+)"', text, re.M)
    if not m or _ver_tuple(m.group(1)) != _ver_tuple(upd["version"]):
        raise RuntimeError("The downloaded file is not the version it should be.")
    compile(text, "kingshot_editor.py", "exec")           # refuse a broken download
    target = Path(__file__).resolve()
    tmp = target.with_name("kingshot_editor.py.new")
    tmp.write_bytes(data)
    if "icon.ico" in upd["assets"]:
        try:
            (target.parent / "icon.ico.new").write_bytes(_http_get(upd["assets"]["icon.ico"]))
            os.replace(target.parent / "icon.ico.new", target.parent / "icon.ico")
        except Exception as e:  # noqa: BLE001
            log(f"update: icon skipped: {e}")
    shutil.copy2(target, target.with_name("kingshot_editor.py.bak"))
    os.replace(tmp, target)
    log(f"update: {APP_VERSION} -> {upd['version']} installed")


class UpdateWorker(QObject):
    """Runs check/apply on a thread; reports back through signals so the window code stays on the GUI thread."""
    checked = Signal(object, str)      # update dict or None, error text ("" if fine)
    applied = Signal(bool, str)        # ok, error text

    def check(self):
        def run():
            try:
                self.checked.emit(check_for_update(), "")
            except Exception as e:  # noqa: BLE001
                log(f"update check failed: {e}")
                self.checked.emit(None, str(e) or e.__class__.__name__)
        threading.Thread(target=run, daemon=True).start()

    def apply(self, upd):
        def run():
            try:
                apply_update(upd)
                self.applied.emit(True, "")
            except Exception as e:  # noqa: BLE001
                log(f"update failed: {e}")
                self.applied.emit(False, str(e) or e.__class__.__name__)
        threading.Thread(target=run, daemon=True).start()



class BenchDialog(QDialog):
    """Runs short FFmpeg tests on the open video (decode only, then decode + each encoder) and writes the results
    to the log, so a slow export can be diagnosed without anyone watching Task Manager."""
    step = Signal(str)
    finished_all = Signal()

    def __init__(self, parent, project):
        super().__init__(parent)
        self.setWindowTitle("Speed test")
        self.setModal(True)
        self.setMinimumWidth(480)
        self.p, self.stop, self.proc = project, False, None
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("<b>Testing how fast this PC handles your video</b>"))
        self.lbl = QLabel("Starting…"); self.lbl.setWordWrap(True)
        bar = QProgressBar(); bar.setRange(0, 0); bar.setTextVisible(False)
        self.note = QLabel("Takes about 2 minutes. Nothing is saved except lines in the log.")
        self.note.setStyleSheet(f"color:{C_DIM}")
        lay.addWidget(self.lbl); lay.addWidget(bar); lay.addWidget(self.note)
        row = QHBoxLayout(); row.addStretch()
        self.b = QPushButton("Cancel"); row.addWidget(self.b); lay.addLayout(row)
        self.b.clicked.connect(self.cancel)
        self.step.connect(self.lbl.setText)
        self.finished_all.connect(self.on_finished)
        keep_awake(True)
        threading.Thread(target=self.run, daemon=True).start()

    def cancel(self):
        self.stop = True
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
        self.reject()

    def on_finished(self):
        keep_awake(False)
        if self.stop:
            return
        self.lbl.setText("Done. Tell Claude the speed test finished: the results are in the log.")
        self.b.setText("Close"); self.b.clicked.disconnect(); self.b.clicked.connect(self.accept)

    def sh(self, args, timeout=60):
        try:
            r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=timeout, creationflags=NO_WINDOW)
            return (r.stdout + r.stderr).strip()
        except Exception as ex:  # noqa: BLE001
            return f"(failed: {ex})"

    def timed(self, name, args):
        if self.stop:
            return
        self.step.emit(f"Testing: {name}…")
        t0 = time.time()
        try:
            self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                         text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
            err = self.proc.communicate(timeout=180)[1]
            code = self.proc.returncode
        except Exception as ex:  # noqa: BLE001
            log(f"BENCH {name}: failed ({ex})")
            return
        el = time.time() - t0
        if self.stop:
            return
        if code != 0:
            log(f"BENCH {name}: FAILED code {code} | {' / '.join(err.strip().splitlines()[-3:])}")
        else:
            log(f"BENCH {name}: {BENCH_SECS / el:.1f}x real time ({el:.1f}s for {BENCH_SECS}s of video)")

    def run(self):
        p = self.p
        log(f"BENCH start | {p.info.get('codec')} {p.info['w']}x{p.info['h']} {p.info.get('fps', 0):.0f} fps | "
            f"cpu threads {os.cpu_count()} | app {APP_VERSION}")
        log("BENCH gpu: " + self.sh(["nvidia-smi", "--query-gpu=name,driver_version,clocks.max.graphics,power.limit,"
                                    "temperature.gpu,utilization.gpu,utilization.encoder,utilization.decoder,pstate",
                                    "--format=csv,noheader"], 15))
        log("BENCH ffmpeg hwaccels: " + " ".join(self.sh([FFMPEG, "-hide_banner", "-hwaccels"]).split()[3:]))
        busy = self.sh(["powershell", "-NoProfile", "-Command",
                        "Get-Process | Sort-Object CPU -Descending | Select-Object -First 6 Name,CPU | "
                        "ForEach-Object { $_.Name + ' ' + [int]$_.CPU } "], 20)
        log("BENCH busiest programs (cpu seconds): " + " | ".join(busy.splitlines()))
        ss = ["-ss", f"{max(0.0, p.dur / 2 - BENCH_SECS / 2):.1f}", "-t", str(BENCH_SECS)]
        src = ["-i", str(p.src)]
        base = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error"]
        null = ["-an", "-f", "null", "-"]
        vf = ["-vf", "format=yuv420p"]
        br = str(int(p.info.get("vbr") or 6_000_000))
        self.timed("decode only, processor", base + ss + src + null)
        self.timed("decode only, graphics card (auto)", base + ["-hwaccel", "auto"] + ss + src + null)
        self.timed("decode only, NVIDIA (cuda)", base + ["-hwaccel", "cuda"] + ss + src + null)
        if p.info.get("codec") == "av1":
            self.timed("decode only, NVIDIA AV1 decoder", base + ["-c:v", "av1_cuvid"] + ss + src + null)
        for hw_name, hw in (("decode on processor", []), ("decode on graphics card", ["-hwaccel", "auto"])):
            for enc in ("hevc_nvenc", "av1_nvenc"):
                self.timed(f"{hw_name} + {enc}", base + hw + ss + src + vf +
                           ["-c:v", enc, "-preset", "p5", "-rc", "vbr", "-b:v", br, "-an", "-f", "null", "-"])
        self.timed("encode only (generated picture) hevc_nvenc", base + ["-f", "lavfi", "-i",
                   f"testsrc2=size={p.info['w']}x{p.info['h']}:rate=60:duration={BENCH_SECS}", "-c:v", "hevc_nvenc",
                   "-preset", "p5", "-b:v", br, "-f", "null", "-"])
        log("BENCH end")
        self.finished_all.emit()


BENCH_SECS = 30


class ProxyDialog(QDialog):
    """Windows can't always play AV1 in the preview (it needs a free add-on from Microsoft). Instead of failing,
    FFmpeg makes a small preview copy once; the export still uses the original file."""

    def __init__(self, parent, project):
        super().__init__(parent)
        self.setWindowTitle("Preparing the preview")
        self.setModal(True)
        self.setMinimumWidth(460)
        self.p, self.out, self.hwdec, self.ex, self.result_path = project, None, True, None, ""
        PREVIEWS.mkdir(parents=True, exist_ok=True)
        clean_previews()
        self.out = PREVIEWS / f"{Path(project.src).stem[:60]}_preview.mp4"
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("<b>This AV1 video can't be previewed directly on this PC</b>"))
        self.note = QLabel("Making a small preview copy so you can cut and edit. Your export still uses the original "
                           "file in full quality.")
        self.note.setWordWrap(True); self.note.setStyleSheet(f"color:{C_DIM}")
        self.bar = QProgressBar(); self.bar.setRange(0, 1000); self.bar.setTextVisible(False)
        self.eta = QLabel("Starting…")
        lay.addWidget(self.note); lay.addWidget(self.bar); lay.addWidget(self.eta)
        row = QHBoxLayout(); row.addStretch()
        self.b_cancel = QPushButton("Skip the preview"); row.addWidget(self.b_cancel); lay.addLayout(row)
        self.b_cancel.clicked.connect(self.cancel)
        keep_awake(True)
        self.launch()

    def launch(self):
        self.ex = Exporter(proxy_command(self.p, self.out, self.hwdec), max(1.0, self.p.dur))
        self.ex.progress.connect(self.on_progress)
        self.ex.done.connect(self.finished_)
        self.ex.start()

    def on_progress(self, f, speed):
        self.bar.setValue(int(f * 1000))
        left = (1 - f) * self.p.dur / speed if speed > 0.01 else None
        self.eta.setText(f"{f * 100:.0f}%" + (f"  ·  about {dur_words(left)} left" if left else ""))

    def cancel(self):
        if self.ex:
            self.ex.cancel()
        self.reject()

    def finished_(self, code, tail):
        if self.ex.cancelled:
            self.out.unlink(missing_ok=True)
            return
        if code != 0 and self.hwdec:
            self.hwdec = False
            return self.launch()
        keep_awake(False)
        if code == 0 and self.out.exists():
            self.result_path = str(self.out)
            self.accept()
        else:
            self.reject()


def purge_song_files(paths):
    """Delete YouTube songs the app downloaded. Only files inside the app's own download folders."""
    left = []
    for p in paths:
        try:
            pp = Path(p).resolve()
            if not (DOWNLOADS.resolve() in pp.parents or MUSIC_DIR.resolve() in pp.parents):
                continue
            pp.unlink(missing_ok=True)
        except OSError:
            left.append(p)
    return left


def yt_url(txt):
    txt = txt.strip()
    return txt if txt.lower().startswith("http") else "https://" + txt


def yt_env():
    # yt-dlp prints UTF-8; without this a Windows console code page garbles the file path we read back
    return {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}


def yt_base():
    tools = APP_DIR.parent / "tools"
    deno = tools / ("deno.exe" if os.name == "nt" else "deno")
    args = [sys.executable.replace("pythonw.exe", "python.exe"), "-m", "yt_dlp", "--no-playlist"]
    if deno.exists():
        args += ["--js-runtimes", f"deno:{deno}"]
    return args


def yt_command(url, out_dir, limit=None):
    """limit: only fetch the first `limit` seconds of the song (when the video is much shorter than the song)."""
    # Only downloads the best audio; the app converts it to MP3 itself (with real progress). "--print" makes
    # yt-dlp quiet, so --no-quiet + a progress template are what bring the progress lines back.
    args = yt_base() + ["--newline", "--no-quiet", "--progress", "-f", "bestaudio/best",
                        "--progress-template",
                        "download:DLPROG %(progress.downloaded_bytes)s %(progress.total_bytes)s "
                        "%(progress.total_bytes_estimate)s %(progress.speed)s %(progress.eta)s",
                        "-o", str(Path(out_dir) / "%(title).100B [%(id)s].%(ext)s"), "--print", "after_move:filepath",
                        "--no-simulate", "--restrict-filenames",
                        # Windows (antivirus scanning the new file) can lock the .part file when yt-dlp renames it:
                        # write straight to the final name, and wait longer if a lock still happens
                        "--no-part", "--file-access-retries", "20", "--retry-sleep", "file_access:2"]
    # 2.6.6: no --download-sections. That hands the download to FFmpeg, which YouTube throttles to ~2x real time
    # (measured: 5 min of audio took 168 s). yt-dlp's own downloader runs at full speed (~100 MB in 25 s), so the
    # worker downloads normally and stops once enough of the song has arrived (`stop_frac`).
    return args + [url]


def yt_update():
    """YouTube changes often and an old yt-dlp stops working: update it in place (needs internet)."""
    uv = APP_DIR.parent / "tools" / ("uv.exe" if os.name == "nt" else "uv")
    if not uv.exists():
        return "downloader updater not found (run Setup again)"
    py = sys.executable.replace("pythonw.exe", "python.exe")
    try:
        r = subprocess.run([str(uv), "pip", "install", "--python", py, "--upgrade", "yt-dlp[default]"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=240,
                           creationflags=NO_WINDOW, env={**yt_env(), "UV_NO_CONFIG": "1"})
        return "" if r.returncode == 0 else (r.stderr or r.stdout)[-300:]
    except Exception as ex:  # noqa: BLE001
        return str(ex)


def yt_needs_update(text):
    t = text.lower()
    return not any(k in t for k in ("sign in", "bot", "private video", "unavailable", "copyright", "members-only",
                                    "confirm your age", "age-restricted", "geo", "no such file", "no module named",
                                    "winerror", "permission denied", "access is denied", "disk", "space"))


TIME_RE = __import__("re").compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")


def log_line(msg):
    log(msg[:600])


def write_yt_log(lines):
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        YT_LOG.write_text("\n".join(lines)[-20000:], encoding="utf-8")
    except OSError:
        pass


# Words in a description / channel name that point to a record label or a rights holder.
LABELS = ("records", "recordings", "music group", "entertainment", "sony", "universal", "warner", "atlantic",
          "columbia", "interscope", "republic", "capitol", "def jam", "rca", "island", "epic", "emi", "bmg",
          "believe", "distrokid", "tunecore", "cd baby", "the orchard", "ingrooves", "vevo")
FREE_WORDS = ("no copyright", "copyright free", "copyright-free", "royalty free", "royalty-free", "nocopyrightsounds",
              "free to use", "creative commons")


def analyze_copyright(info):
    """Best-effort guess from YouTube's own metadata. Returns (level, reasons): level is 'likely',
    'possible' or 'none'. A real Content ID check is not possible offline, so this is only a hint."""
    desc = (info.get("description") or "")
    dl = desc.lower()
    chan = (info.get("channel") or info.get("uploader") or "")
    cl = chan.lower()
    lic = (info.get("license") or "")
    strong, weak, good = [], [], []
    if "provided to youtube by" in dl:
        strong.append("The description says \u201cProvided to YouTube by\u201d, which means a label or distributor "
                      "delivered this song, so it is almost certainly claimed.")
    if "auto-generated by youtube" in dl:
        strong.append("Auto-generated by YouTube: an official release from a label.")
    if cl.endswith(" - topic"):
        strong.append("It is on an artist \u201cTopic\u201d channel, which only holds label releases.")
    if "vevo" in cl:
        strong.append("It comes from a VEVO channel (label content).")
    if info.get("track") or info.get("album") or (info.get("artists") and info.get("categories") == ["Music"]):
        strong.append("YouTube has song data attached (track / album / artist), so it is registered as a song.")
    if ("\u2117" in desc or "\u00a9" in desc) and any(w in dl for w in LABELS):
        strong.append("The description has a \u2117 / \u00a9 line naming a label.")
    elif "\u2117" in desc or "\u00a9" in desc:
        weak.append("The description has a \u2117 / \u00a9 copyright line.")
    if any(w in cl for w in LABELS):
        weak.append(f"The channel name (\u201c{chan}\u201d) looks like a label.")
    if "official" in cl and "no copyright" not in cl:
        weak.append(f"The channel (\u201c{chan}\u201d) calls itself official.")
    if "all rights reserved" in dl:
        weak.append("The description says \u201cAll rights reserved\u201d.")
    if (info.get("categories") or []) == ["Music"] and not strong:
        weak.append("It is in YouTube\u2019s Music category, where most uploads are somebody\u2019s song.")
    if "creative commons" in lic.lower():
        good.append("Its license is Creative Commons (you may need to credit the author).")
    if any(w in dl or w in (info.get("title") or "").lower() for w in FREE_WORDS):
        good.append("It is described as copyright free. That is the uploader\u2019s claim and is not checked.")
    if strong:
        return "likely", strong + weak
    if good:
        weak = [w for w in weak if "Music category" not in w]
        if not weak:
            return "none", good
    if weak:
        return "possible", weak + good
    return "none", good


class YtInfoWorker(QObject):
    done = Signal(int, str, str)      # code, json text, error text

    def __init__(self, url):
        super().__init__()
        self.url, self.cancelled = url, False

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        log, updated, code, out, err = [], False, -1, "", ""
        for _ in range(2):
            try:
                r = subprocess.run(yt_base() + ["-J", "--skip-download", "--no-warnings", self.url],
                                   capture_output=True, text=True, encoding="utf-8", errors="replace",
                                   stdin=subprocess.DEVNULL, timeout=120, creationflags=NO_WINDOW, env=yt_env())
                code, out, err = r.returncode, r.stdout, r.stderr
            except Exception as ex:  # noqa: BLE001
                code, out, err = -1, "", str(ex)
            log += [f"--- info check (exit {code}) ---", err.strip()]
            if code == 0 or updated or self.cancelled or not yt_needs_update(err):
                break
            updated = True
            log.append("updating yt-dlp: " + (yt_update() or "ok"))
        write_yt_log(log)
        self.done.emit(code, out, err.strip()[-400:])

    def cancel(self):
        self.cancelled = True


class YtWorker(QObject):
    progress = Signal(float, str)
    done = Signal(int, str, str)     # code, file path, error text

    def __init__(self, args, fallback=None, limit=None, stop_frac=None):
        super().__init__()
        self.args, self.proc, self.cancelled = args, None, False
        self.fallback, self.limit = fallback, limit     # fallback: same download without the time limit
        self.stop_frac, self.early, self.dest = stop_frac, False, ""   # stop the download after this share of the file

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _attempt(self, log):
        path, errs = "", []
        self.last_out, self.stalled = time.time(), False
        proc = self.proc = subprocess.Popen(self.args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                            stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                                            creationflags=NO_WINDOW, env=yt_env())
        log_line("yt-dlp start: " + subprocess.list2cmdline(self.args))
        threading.Thread(target=self._watchdog, args=(proc,), daemon=True).start()
        for line in self.proc.stdout:
            self.last_out = time.time()
            line = line.strip()
            if line.startswith("DLPROG"):
                self._dl_progress(line.split()[1:])
                continue
            if "time=" in line and "size=" in line and self.limit:
                # a trimmed download goes through FFmpeg, which has no yt-dlp progress: read its own time counter
                m = TIME_RE.search(line)
                if m:
                    t = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
                    frac = clamp(t / self.limit, 0, 1)
                    self.progress.emit(frac, f"Step 1 of 2: downloading… {frac * 100:.0f}%")
                continue
            log.append(line)
            log_line("yt-dlp: " + line)
            if line.startswith("[download] Destination:"):
                self.dest = line.split("Destination:", 1)[1].strip()
            if line.startswith("[youtube]") and "Downloading" in line:
                self.progress.emit(0.0, "Step 1 of 2: getting the video from YouTube…")
            elif line and not line.startswith("[") and Path(line).is_file() and not line.endswith((".part", ".ytdl")):
                path = line
            elif "ERROR" in line or "WARNING" in line:
                errs.append(line)
        code = self.proc.wait()
        if self.early and not self.cancelled and self.dest and Path(self.dest).is_file():
            code, path = 0, self.dest      # stopped on purpose once enough of the song was there
        log_line(f"yt-dlp end: code {code}{' (stopped early, enough downloaded)' if self.early else ''}"
                 f"{' (stalled, stopped)' if self.stalled else ''}")
        if self.stalled:
            errs.append("ERROR: the download stopped responding.")
        return code, path, errs

    def _watchdog(self, proc):
        """A download that says nothing for 2 minutes is stuck: stop it (a trimmed one then retries in full)."""
        while proc.poll() is None:
            time.sleep(5)
            if proc is self.proc and not self.cancelled and time.time() - self.last_out > 120:
                self.stalled = True
                proc.kill()
                return

    def _dl_progress(self, v):
        def num(x):
            try:
                return float(x)
            except (ValueError, TypeError):
                return 0.0
        done, total, est, speed, eta = (num(x) for x in (v + ["0"] * 5)[:5])
        total = total or est
        frac = clamp(done / total, 0, 1) if total else 0.0
        if self.stop_frac and total:
            if frac >= self.stop_frac and not self.early and self.proc and self.proc.poll() is None:
                self.early = True
                self.proc.kill()
            frac = clamp(frac / self.stop_frac, 0, 1)      # the bar fills up to the part we actually need
        txt = f"Step 1 of 2: downloading… {frac * 100:.0f}%"
        if speed:
            txt += f"  ·  {speed / 1e6:.1f} MB/s"
        if eta:
            txt += f"  ·  about {dur_words(eta)} left"
        self.progress.emit(frac, txt)

    def _convert(self, src, log):
        """Audio file -> MP3 with ffmpeg, reporting real progress from its own time counter."""
        src = Path(src)
        out = src.with_suffix(".mp3")
        if out == src:
            out = src.with_name(src.stem + "_mp3.mp3")
        try:
            total = probe(str(src))["duration"]
        except Exception:  # noqa: BLE001
            total = 0.0
        args = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src), "-vn", "-map_metadata", "0"]
        if self.limit:          # never convert more than the video needs, even if the whole song came down
            args += ["-t", str(int(self.limit))]
            total = min(total, self.limit) if total else self.limit
        args += ["-c:a", "libmp3lame", "-q:a", "0", "-progress", "pipe:1", "-nostats", str(out)]
        log.append(" ".join(args))
        self.progress.emit(0.0, "Step 2 of 2: converting to MP3… 0%")
        self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        errs = []
        for line in self.proc.stdout:
            line = line.strip()
            if line.startswith("out_time_us=") and total > 0:
                try:
                    frac = clamp(int(line.split("=")[1]) / 1e6 / total, 0, 1)
                    self.progress.emit(frac, f"Step 2 of 2: converting to MP3… {frac * 100:.0f}%")
                except ValueError:
                    pass
            elif "=" not in line and line:
                errs.append(line)
        code = self.proc.wait()
        log += errs
        if code == 0 and out.exists():
            try:
                src.unlink()
            except OSError:
                pass
            return str(out), []
        return "", ["ERROR: converting to MP3 failed. " + " ".join(errs[-2:])]

    def _run(self):
        log, updated = [" ".join(self.args)], False
        try:
            while True:
                code, path, errs = self._attempt(log)
                if code != 0 and self.fallback and not self.cancelled:
                    log.append("short download failed, trying the whole song")
                    self.args, self.fallback = self.fallback, None
                    self.progress.emit(0.0, "Starting…")
                    continue
                if code == 0 and path or self.cancelled or updated or not yt_needs_update("\n".join(errs)):
                    break
                updated = True              # likely an outdated downloader: update it once and retry
                self.progress.emit(0.0, "Updating the downloader, then trying again…")
                log.append("updating yt-dlp: " + (yt_update() or "ok"))
                self.progress.emit(0.0, "Starting…")
            if code == 0 and path and not self.cancelled:
                if not FFMPEG:
                    errs = ["ERROR: FFmpeg was not found."]; path = ""
                else:
                    path, errs = self._convert(path, log)
                    code = 0 if path else 1
        except Exception as ex:  # noqa: BLE001
            write_yt_log(log + [str(ex)])
            self.done.emit(-1, "", str(ex))
            return
        write_yt_log(log)
        if code == 0 and not path:
            errs.append("The download finished but the MP3 file was not found.")
        self.done.emit(code, path, "\n".join(errs[-4:]))

    def cancel(self):
        self.cancelled = True
        if self.proc and self.proc.poll() is None:
            self.proc.kill()


class YtDialog(QDialog):
    MINIMIZED = 2                   # exec() result when the dialog is tucked away while the download carries on
    progressed = Signal(float, str)

    def __init__(self, parent, url="", limit=None):
        super().__init__(parent)
        self.limit = limit          # seconds of song the video can use (None: no video open, take it all)
        self.setWindowTitle("Add a song from YouTube")
        self.setMinimumWidth(520)
        self.win, self.result_path, self.w, self.iw, self.warn_text = parent, None, None, None, ""
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("<b>Paste a YouTube link</b>"))
        self.edit = QLineEdit(url); self.edit.setPlaceholderText("https://www.youtube.com/watch?v=…")
        self.edit.returnPressed.connect(self.go)
        lay.addWidget(self.edit)
        self.bar = QProgressBar(); self.bar.setRange(0, 1000); self.bar.setTextVisible(False); self.bar.hide()
        self.msg = QLabel("The song is saved as an MP3 in your Music folder and added to the Music track.")
        self.msg.setWordWrap(True); self.msg.setStyleSheet(f"color:{C_DIM}")
        note = QLabel("Note: YouTube may block or mute uploads that use copyrighted music. The app checks the link "
                      "for signs of that first, but it can only guess. Use songs you're allowed to use.")
        note.setWordWrap(True); note.setStyleSheet(f"color:{C_DIM}; font-size:11px")
        lay.addWidget(self.bar); lay.addWidget(self.msg); lay.addWidget(note)
        row = QHBoxLayout(); row.addStretch()
        self.b_cancel = QPushButton("Cancel"); self.b_go = QPushButton("⬇ Download"); self.b_go.setObjectName("primary")
        self.b_min = QPushButton("Minimize"); self.b_min.hide()
        self.b_min.setToolTip("Keep editing while the song downloads; a small bar under Add music shows the progress")
        self.b_min.clicked.connect(self.minimize)
        self.minimized = False
        row.addWidget(self.b_cancel); row.addWidget(self.b_min); row.addWidget(self.b_go); lay.addLayout(row)
        self.b_cancel.clicked.connect(self.cancel); self.b_go.clicked.connect(self.go)

    def go(self):
        url = self.edit.text().strip()
        if not YT_RE.match(url):
            self.msg.setText("That doesn't look like a YouTube link. Copy the address from the video's page.")
            return
        url = yt_url(url)
        self.edit.setEnabled(False); self.b_go.setEnabled(False); self.bar.show(); self.bar.setRange(0, 0)
        self.msg.setText("Checking the link…")
        self.iw = YtInfoWorker(url)
        self.iw.done.connect(self.info_done)
        self.iw.start()

    def minimize(self):
        self.minimized = True
        self.done(self.MINIMIZED)

    def reset_ui(self, text):
        self.b_min.hide()
        self.edit.setEnabled(True); self.b_go.setEnabled(True); self.bar.hide(); self.bar.setRange(0, 1000)
        self.msg.setText(text)

    def info_done(self, code, text, err):
        if self.iw.cancelled:
            return
        url = yt_url(self.edit.text())
        info = None
        if code == 0:
            try:
                info = json.loads(text)
            except ValueError:
                info = None
        if info:
            level, reasons = analyze_copyright(info)
            if level != "none":
                self.warn_text = ("Likely copyrighted: " if level == "likely" else "Possibly copyrighted: ") + \
                                 " ".join(reasons[:2])
                box = QMessageBox(self)
                box.setIcon(QMessageBox.Warning)
                box.setWindowTitle("Copyright check")
                box.setText(f"<b>{'This song is probably copyrighted' if level == 'likely' else 'This song might be copyrighted'}"
                            f"</b><br>{info.get('title', '')}")
                box.setInformativeText("\n".join("\u2022 " + r for r in reasons) +
                                       "\n\nThis is a best guess from YouTube's own info. Only YouTube can tell for "
                                       "sure, and only when you upload. If it is claimed, your video may be muted, "
                                       "blocked or have ads go to the owner.")
                go_on = box.addButton("Download anyway", QMessageBox.AcceptRole)
                no = box.addButton("Cancel", QMessageBox.RejectRole)
                box.setDefaultButton(no)
                box.exec()
                if box.clickedButton() is not go_on:
                    return self.reset_ui("Cancelled. Paste another link, or press Download to try this one again.")
            else:
                self.warn_text = ""
        else:
            self.warn_text = ""       # no info: the download below will show the real error if the link is bad
        # Download into the app's own folder first: Windows (Defender "Controlled folder access", OneDrive,
        # antivirus) can refuse python.exe's temp-file rename inside Music. The finished MP3 is moved after.
        out_dir = DOWNLOADS
        out_dir.mkdir(parents=True, exist_ok=True)
        clean_download_leftovers()
        self.bar.setRange(0, 1000); self.bar.setValue(0)
        dur = (info or {}).get("duration") or 0
        # Trimming while downloading goes through FFmpeg (no normal progress, slower to start), so only do it when the
        # song is much longer than needed (a long mix). Otherwise download normally and cut it while converting.
        lim = self.limit if (self.limit and dur > self.limit + 60) else None
        cut_at_source = bool(lim and dur > max(lim * 3, 1200))
        if lim:
            self.msg.setText(f"Starting… this song is {dur_words(dur)} long, but your video only needs the first "
                             f"{dur_words(lim)}, so only that part is downloaded.")
        else:
            self.msg.setText("Starting the download…" + (f"\nThis song is {dur_words(dur)} long, so it will take a "
                                                        "while." if dur > 900 else ""))
        # the whole-song retry is pointless for a huge mix (a 12 h video is gigabytes): only for songs up to 2 hours
        # download at full speed and stop once the needed part (plus a safety margin) has arrived
        stop = min(1.0, (lim + 30) / dur * 1.05) if (cut_at_source and dur) else None
        self.w = YtWorker(yt_command(url, out_dir), None, lim, stop)
        log(f"YouTube download: {url} | song {dur:.0f}s, video allows {lim}, cut while downloading: {cut_at_source}")
        self.w.progress.connect(self.on_progress)
        self.w.done.connect(self.finished_)
        self.w.start()
        self.b_min.show()

    def on_progress(self, f, t):
        self.bar.setRange(0, 1000); self.bar.setValue(int(f * 1000))
        self.msg.setText(t)
        self.progressed.emit(f, t)

    @staticmethod
    def to_music_folder(path):
        """Move the finished MP3 into Music\\Kingshot Editor. If that isn't allowed, keep using it where it is."""
        try:
            dest_dir = MUSIC_DIR
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / Path(path).name
            if dest.exists():            # same video downloaded before: reuse that copy
                try:
                    os.remove(path)
                except OSError:
                    pass
                return str(dest)
            shutil.move(path, str(dest))
            return str(dest)
        except OSError:
            return path

    def finished_(self, code, path, err):
        if self.w.cancelled:
            return
        if code == 0 and path:
            self.result_path = self.to_music_folder(path)
            self.accept()
            return
        self.edit.setEnabled(True); self.b_go.setEnabled(True); self.bar.hide(); self.b_min.hide()
        if self.minimized:               # it failed while tucked away: bring the dialog back so the error is seen
            self.minimized = False
            self.progressed.emit(-1.0, "")
            self.show(); self.raise_()
        hint = ""
        if "WinError 5" in err or "Permission denied" in err or "Access is denied" in err:
            hint = ("\nWindows blocked saving the file (antivirus or \u201cControlled folder access\u201d). "
                    "Try again; if it keeps happening, allow the Kingshot Editor in Windows Security.")
        elif "Sign in" in err or "bot" in err:
            hint = "\nYouTube asked for a sign-in. Try again later or another video."
        self.msg.setText("Download failed." + hint + ("\n" + err if err else "") +
                         f"\nIf this keeps happening, send the file {YT_LOG} to whoever is helping you.")
        self.msg.setTextInteractionFlags(Qt.TextSelectableByMouse)

    def cancel(self):
        if self.w:
            self.w.cancel()
        if self.iw:
            self.iw.cancel()
        self.reject()


class ExportDialog(QDialog):
    def __init__(self, parent, project, out_path, encs):
        super().__init__(parent)
        self.encs = list(encs)       # export_plan(): graphics card encoders first, processor last
        enc = self.encs.pop(0)
        self.setWindowTitle("Export")
        self.setModal(True)
        self.setWindowFlag(Qt.WindowMinimizeButtonHint, True)   # the export can run while the editor is minimized
        self.setMinimumWidth(460)
        self.p, self.out, self.enc = project, Path(out_path), enc
        self.modes = decode_plan(project, enc)     # decoding modes, fastest first; steps down if FFmpeg can't use one
        lay = QVBoxLayout(self)
        self.title = QLabel("<b>Exporting your video…</b>")
        self.bar = QProgressBar(); self.bar.setRange(0, 1000); self.bar.setTextVisible(False)
        self.info = QLabel(""); self.info.setStyleSheet(f"color:{C_DIM}"); self.info.setWordWrap(True)
        self.eta = QLabel("Starting…")
        lay.addWidget(self.title); lay.addWidget(self.bar); lay.addWidget(self.eta); lay.addWidget(self.info)
        row = QHBoxLayout(); row.addStretch()
        self.b_cancel = QPushButton("Cancel")
        self.b_min = QPushButton("Minimize")
        self.b_min.clicked.connect(self.minimize_all)
        self.b_why = QPushButton("Why not the graphics card?"); self.b_why.hide()
        self.b_why.clicked.connect(self.show_why)
        row.addWidget(self.b_why)
        self.b_folder = QPushButton("Show in folder"); self.b_play = QPushButton("▶ Play it")
        self.b_close = QPushButton("Close"); self.b_close.setObjectName("primary")
        for b in (self.b_folder, self.b_play, self.b_close):
            b.hide()
        for b in (self.b_min, self.b_cancel, self.b_folder, self.b_play, self.b_close):
            row.addWidget(b)
        lay.addLayout(row)
        self.b_cancel.clicked.connect(self.cancel)
        self.b_close.clicked.connect(self.accept)
        self.b_folder.clicked.connect(self.show_folder)
        self.b_play.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.out))))
        self.ex = None
        keep_awake(True)
        log(f"EXPORT started -> {self.out} | codec setting {project.codec}, plan {[enc] + self.encs} | {project_summary(project)}")
        self.launch()

    def launch(self):
        args, self.total = build_export(self.p, self.out, self.enc, self.modes[0])
        log(f"export attempt: encoder {self.enc} ({enc_name(self.enc)}), decode mode {self.modes[0]} of {self.modes}, "
            f"remaining fallbacks {self.encs}")
        W, H = self.p.W, self.p.H
        sp = f" · {self.p.speed:g}× video speed" if self.p.speed != 1 else ""
        sp += f" · {self.p.mspeed:g}× sound speed" if self.p.mspeed != 1 else ""
        self.info.setText(f"{self.out.name} · {fmt(self.total)} · {W}×{H}{sp}\n{CODEC_LABEL[enc_codec(self.enc)]} · encoding on the {enc_name(self.enc)}")
        self.b_why.setVisible(self.enc.startswith("lib") and bool(_ENC_ERR))
        self.ex = Exporter(args, self.total)
        self.ex.progress.connect(self.on_progress)
        self.ex.done.connect(self.finished_)
        self.ex.start()

    def on_progress(self, f, speed):
        self.bar.setValue(int(f * 1000))
        left = (1 - f) * self.total / speed if speed > 0.01 else None
        self.eta.setText(f"{f * 100:.1f}%" + (f"  ·  about {dur_words(left)} left  ·  {speed:.1f}× real time"
                                              if left is not None else ""))

    def cancel(self):
        if self.ex:
            self.ex.cancel()

    def minimize_all(self):
        """Tuck the whole editor (and this dialog with it) into the taskbar; the export keeps running."""
        w = self.parent()
        if w is not None:
            w.showMinimized()
        else:
            self.showMinimized()

    def changeEvent(self, e):
        if e.type() == QEvent.WindowStateChange and self.isMinimized():
            QTimer.singleShot(0, self._unminimize_self)
        super().changeEvent(e)

    def _unminimize_self(self):
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized)   # not a stray mini-window on the desktop
        self.minimize_all()

    def closeEvent(self, e):
        if self.b_cancel.isVisible():
            self.cancel()
        keep_awake(False)
        super().closeEvent(e)

    def done(self, r):
        keep_awake(False)
        super().done(r)

    def show_why(self):
        lines = "<br>".join(f"<b>{k} – {enc_name(k)}</b>: {v}" for k, v in _ENC_ERR.items()
                          if self.p.codec == "auto" or k.startswith(self.p.codec))
        QMessageBox.information(self, "Graphics card encoding", "None of the graphics card encoders worked on this PC:"
                                f"<br><br>{lines}<br><br>Updating your graphics driver usually fixes this. "
                                "The processor export gives the same result, just slower.")

    def show_folder(self):
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", str(self.out)])
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.out.parent)))

    def finished_(self, code, tail):
        if self.ex.cancelled:
            try:
                self.out.unlink(missing_ok=True)
            except OSError:
                pass
            self.reject()
            return
        if code != 0 and len(self.modes) > 1:
            log(f"decode mode {self.modes[0]} failed: " + (tail.splitlines() or [""])[-1][:250])
            self.modes.pop(0)           # that decoding path failed: same encoder, next (slower) decoding mode
            self.bar.setValue(0)
            self.eta.setText("That decoding mode didn't work, trying the next one…")
            return self.launch()
        if code != 0 and self.encs:
            # this encoder gave up: try the next graphics-card encoder, then the processor
            _ENC_ERR[self.enc] = "failed during export: " + (tail.splitlines() or [""])[-1][:250]
            self.enc = self.encs.pop(0)
            self.modes = decode_plan(self.p, self.enc)
            self.bar.setValue(0)
            self.eta.setText(f"That encoder failed, trying the {enc_name(self.enc)}…")
            return self.launch()
        self.b_cancel.hide(); self.b_min.hide()
        keep_awake(False)
        try:
            QApplication.alert(self.parent() or self)       # flash the taskbar button if the editor is minimized
        except Exception:
            pass
        log("EXPORT " + ("finished OK" if code == 0 else "FAILED") + f" with {self.enc}")
        if code == 0:
            self.p.exported = True
            self.bar.setValue(1000)
            size = self.out.stat().st_size / 1048576 if self.out.exists() else 0
            self.title.setText("<b>Done! 🎉</b>")
            self.eta.setText("")
            kind = "YouTube Shorts" if self.p.format == "short" else "YouTube"
            self.info.setText(f"{self.out.name} · {size:,.1f} MB · {self.p.W}×{self.p.H} · ready for {kind}")
            for b in (self.b_folder, self.b_play, self.b_close):
                b.show()
        else:
            self.title.setText("<b>Export failed</b>")
            self.eta.setText("")
            self.info.setText("FFmpeg said:\n" + (tail or "unknown error"))
            self.info.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.b_close.show()


# ---------------------------------------------------------------- main window
def card(title):
    f = QFrame(); f.setObjectName("card")
    lay = QVBoxLayout(f); lay.setContentsMargins(12, 10, 12, 12); lay.setSpacing(6)
    if title:
        h = QLabel(title.upper()); h.setObjectName("cardTitle"); lay.addWidget(h)
    return f, lay


def slider(lo, hi, val):
    s = QSlider(Qt.Horizontal); s.setRange(lo, hi); s.setValue(val); s.setFocusPolicy(Qt.NoFocus)
    return s


class Main(QMainWindow):
    def __init__(self):
        super().__init__()
        self.p = Project()
        self.undo_stack, self.redo_stack = [], []
        self.settings = QSettings("KingshotEditor", "KingshotEditor")
        self.updater = UpdateWorker(self)
        self.updater.checked.connect(self.on_update_checked)
        self.updater.applied.connect(self.on_update_applied)
        self._upd_manual = False
        self._upd = None
        QTimer.singleShot(5000, self.updater.check)       # quiet check a few seconds after start
        clean_download_leftovers()
        clean_previews()
        # manual blur boxes are an extra: hidden until "Blur tools" is ticked (remembered)
        self.blur_tools = str(self.settings.value("blurTools", "false")).lower() in ("true", "1")
        self.setWindowTitle(APP_NAME)
        self.resize(1280, 860)
        self.setAcceptDrops(True)

        # media
        self.player = QMediaPlayer(self)
        self.aout = QAudioOutput(self); self.player.setAudioOutput(self.aout)
        self.sink = QVideoSink(self); self.player.setVideoSink(self.sink)
        self.sink.videoFrameChanged.connect(self.on_frame)
        self.player.mediaStatusChanged.connect(self.on_status)
        self.player.errorOccurred.connect(self.on_player_error)
        self.player.playbackStateChanged.connect(self.on_state)
        self.mplayers = {}      # music clip id -> (player, audio output)
        self.vaplayer = None    # sound-only player for normal-speed video sound
        self.preview_mult = 1.0  # "Watch Nx": preview-only playback speed, doesn't affect the export
        self._frame_t = 0.0
        self._seek_target = None
        self._first_load = False
        self._await_first = False

        # toolbar
        tb = QToolBar(); tb.setMovable(False); tb.setFloatable(False); self.addToolBar(tb)
        brand = QLabel(f"  ✂ Kingshot <span style='color:{C_ACCENT}'>Editor</span>   "); brand.setObjectName("brand")
        tb.addWidget(brand)
        self.b_open = self.tbtn(tb, "📂 Open video", self.open_video)
        self.b_music = self.tbtn(tb, "🎵 Add music ▾", self.show_music_menu)
        self.b_music.setToolTip("From your computer, or paste/press Ctrl+V with a YouTube link")
        # download progress: a thin rounded bar drawn over the bottom edge of the button, so the button keeps its size
        self.yt_bar = QProgressBar(self.b_music); self.yt_bar.setRange(0, 1000); self.yt_bar.setTextVisible(False)
        self.yt_bar.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.yt_bar.setStyleSheet(f"QProgressBar{{background:rgba(0,0,0,110);border:none;border-radius:2px;}}"
                                  f"QProgressBar::chunk{{background:{C_MUSIC.name()};border-radius:2px;}}")
        self.yt_bar.hide()
        self.b_music.installEventFilter(self)
        self.yt_bg = None
        self.b_undo = self.tbtn(tb, "↶ Undo", self.undo)
        self.b_redo = self.tbtn(tb, "↷ Redo", self.redo)
        self.tbtn(tb, "📁", lambda: open_folder(yt_ready_dir())).setToolTip("Open the YT Ready folder (your exports)")
        sp = QWidget(); sp.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred); tb.addWidget(sp)
        self.l_len = QLabel(""); self.l_len.setStyleSheet(f"color:{C_DIM}; margin-right:8px"); tb.addWidget(self.l_len)
        self.tbtn(tb, "?", self.show_help).setToolTip("Keyboard shortcuts and tips")
        self.b_export = self.tbtn(tb, "⬇ Export for YouTube", self.export)
        self.b_export.setObjectName("primary")

        # left column: preview on top, timeline under it.  Right: side panel, full height.
        self.view = VideoView(self)
        side_inner = QWidget(); side_inner.setObjectName("sideInner")
        self.side = QVBoxLayout(side_inner)
        self.side.setContentsMargins(16, 14, 16, 16); self.side.setSpacing(0)
        self.ctx_holder = QVBoxLayout(); self.ctx_holder.setContentsMargins(0, 0, 0, 14)
        self.side.addLayout(self.ctx_holder)
        self.build_frame_card(); self.build_sound_card()
        self.side.addStretch()
        scroll = QScrollArea(); scroll.setWidget(side_inner); scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # a bit wider than the 300px the content is laid out for, so the vertical scrollbar
        # gets its own lane instead of overlapping the panel's text and dropdowns
        scroll.setFixedWidth(300 + 24); scroll.setObjectName("side"); scroll.setFrameShape(QFrame.NoFrame)

        bottom = QFrame(); bottom.setObjectName("bottom")
        bl = QVBoxLayout(bottom); bl.setContentsMargins(12, 8, 12, 8); bl.setSpacing(6)
        tr = QHBoxLayout()
        self.b_play = QPushButton("▶"); self.b_play.setFixedWidth(44); self.b_play.clicked.connect(self.toggle_play)
        self.b_play.setFocusPolicy(Qt.NoFocus)
        self.c_preview = QComboBox(); self.c_preview.setFocusPolicy(Qt.NoFocus); self.c_preview.setFixedWidth(100)
        self.c_preview.setToolTip("Watch the preview faster - doesn't change the exported video's speed")
        for v in (1, 1.5, 2, 4, 8):
            self.c_preview.addItem("Watch 1×" if v == 1 else f"Watch {v:g}×", v)
        self.c_preview.currentIndexChanged.connect(self.preview_speed_changed)
        self.l_time = QLabel("0:00.0 / 0:00.0"); self.l_time.setObjectName("time")
        tr.addWidget(self.b_play); tr.addWidget(self.c_preview); tr.addWidget(self.l_time); tr.addStretch()
        self.k_blur = QCheckBox("Blur tools"); self.k_blur.setFocusPolicy(Qt.NoFocus)
        self.k_blur.setToolTip("Show the Blur track and let you drag blur boxes on the video")
        self.k_blur.setChecked(self.blur_tools)
        self.k_blur.toggled.connect(self.blur_tools_toggled)
        tr.addWidget(self.k_blur); tr.addSpacing(10)
        zl = QLabel("Zoom"); zl.setStyleSheet(f"color:{C_DIM}"); tr.addWidget(zl)
        for text, fn, tip in (("−", lambda: self.tl.zoom_by(0.5), "Zoom out (or mouse wheel on the timeline)"),
                              ("+", lambda: self.tl.zoom_by(2, self.cur_time()), "Zoom in around the playhead"),
                              ("Fit", lambda: self.tl.set_view(0, 1e12), "Show the whole video")):
            b = QPushButton(text); b.setToolTip(tip); b.clicked.connect(fn); b.setFocusPolicy(Qt.NoFocus)
            b.setFixedWidth(44 if text == "Fit" else 32); tr.addWidget(b)
        bl.addLayout(tr)
        self.tl = Timeline(self); bl.addWidget(self.tl)
        self.tl_scroll = QScrollBar(Qt.Horizontal); self.tl_scroll.setFocusPolicy(Qt.NoFocus)
        self.tl_scroll.valueChanged.connect(self.scroll_moved)
        bl.addWidget(self.tl_scroll)
        self.tl.on_view = self.view_changed
        self.view_changed()

        left = QWidget(); ll = QVBoxLayout(left); ll.setContentsMargins(0, 0, 0, 0); ll.setSpacing(0)
        ll.addWidget(self.view, 1); ll.addWidget(bottom)
        root = QWidget(); rl = QHBoxLayout(root); rl.setContentsMargins(0, 0, 0, 0); rl.setSpacing(0)
        rl.addWidget(left, 1); rl.addWidget(scroll)
        self.setCentralWidget(root)

        self.toast_lbl = QLabel(self); self.toast_lbl.setObjectName("toast"); self.toast_lbl.hide()
        self.toast_timer = QTimer(self, singleShot=True); self.toast_timer.timeout.connect(self.toast_lbl.hide)

        self.shortcuts()
        self.timer = QTimer(self); self.timer.timeout.connect(self.tick); self.timer.start(16)
        self.changed()
        if not (FFMPEG and FFPROBE):
            QTimer.singleShot(300, lambda: QMessageBox.warning(
                self, APP_NAME, "FFmpeg was not found. Please run Setup again to repair the install."))

    # ---- timeline view
    def blur_tools_toggled(self, on):
        self.blur_tools = on
        self.settings.setValue("blurTools", "true" if on else "false")
        if not on and self.p.active and self.p.active[0] == "blur":
            self.p.active = None
        self.tl.relayout()
        if not on and self.p.blurs:
            self.toast("Blurs you already added are still applied to the video.")
        self.changed()

    def view_changed(self):
        v0, ln = self.tl.view()
        d = self.p.dur
        zoomed = bool(d) and ln < d - 1e-6
        self.tl_scroll.setVisible(zoomed)
        if zoomed:
            self.tl_scroll.blockSignals(True)
            self.tl_scroll.setRange(0, int((d - ln) * 10)); self.tl_scroll.setPageStep(max(1, int(ln * 10)))
            self.tl_scroll.setSingleStep(max(1, int(ln)))
            self.tl_scroll.setValue(int(v0 * 10))
            self.tl_scroll.blockSignals(False)

    def scroll_moved(self, v):
        _, ln = self.tl.view()
        self.tl.set_view(v / 10, ln)

    # ---- ui building
    def tbtn(self, tb, text, fn):
        b = QPushButton(text); b.clicked.connect(fn); b.setFocusPolicy(Qt.NoFocus); tb.addWidget(b)
        return b

    def section(self, title):
        f = QFrame(); f.setObjectName("section")
        lay = QVBoxLayout(f); lay.setContentsMargins(0, 14, 0, 14); lay.setSpacing(8)
        h = QLabel(title.upper()); h.setObjectName("cardTitle"); lay.addWidget(h)
        self.side.addWidget(f)
        return lay

    def slider_row(self, lay, name, lo, hi, val, show):
        """Label on the left, value on the right, slider underneath."""
        row = QHBoxLayout(); row.setContentsMargins(0, 4, 0, 0)
        l1 = QLabel(name); l2 = QLabel(); l2.setObjectName("val")
        row.addWidget(l1); row.addStretch(); row.addWidget(l2)
        s = slider(lo, hi, val)
        upd = lambda v: l2.setText(show(v))
        s.valueChanged.connect(upd); upd(val)
        lay.addLayout(row); lay.addWidget(s)
        return s, (l1, l2)

    def build_frame_card(self):
        l = self.section("Format")
        self.c_fmt = QComboBox(); self.c_fmt.setFocusPolicy(Qt.NoFocus); self.c_fmt.setMinimumContentsLength(10)
        for k, (w, h, t) in FORMATS.items():
            self.c_fmt.addItem(f"{t}", k)
            self.c_fmt.setItemData(self.c_fmt.count() - 1, f"{w}×{h}", Qt.ToolTipRole)
        self.c_fmt.addItem(SOURCE_LABEL, SOURCE_FORMAT)
        self.c_fmt.setItemData(self.c_fmt.count() - 1, "Keeps the video's exact width and height: nothing is cropped, "
                               "padded or scaled", Qt.ToolTipRole)
        self.c_fmt.currentIndexChanged.connect(self.frame_ui_changed)
        l.addWidget(self.c_fmt)
        self.l_res = QLabel(); self.l_res.setObjectName("dim"); self.l_res.setWordWrap(True); l.addWidget(self.l_res)
        self.c_codec = QComboBox(); self.c_codec.setFocusPolicy(Qt.NoFocus)
        self.c_codec.addItem("Recommended", "auto")
        self.c_codec.addItem("AV1", "av1")
        self.c_codec.addItem("HEVC", "hevc")
        self.c_codec.addItem("H.264", "h264")
        saved = self.settings.value("codec2", "auto")
        self.c_codec.setCurrentIndex(max(0, self.c_codec.findData(saved)))
        self.c_codec.currentIndexChanged.connect(self.codec_changed)
        l.addWidget(self.c_codec)
        self.c_quality = QComboBox(); self.c_quality.setFocusPolicy(Qt.NoFocus)
        self.c_quality.addItem("Same quality as the original", "same")
        self.c_quality.addItem("Smaller file", "small")
        self.c_quality.setCurrentIndex(max(0, self.c_quality.findData(self.settings.value("quality", "same"))))
        self.c_quality.currentIndexChanged.connect(self.codec_changed)
        l.addWidget(self.c_quality)
        l = self.section("Fit")
        self.c_frame = QComboBox(); self.c_frame.setFocusPolicy(Qt.NoFocus); self.c_frame.setMinimumContentsLength(10)
        for k, t in (("blurbg", "Blurred background"), ("fill", "Fill the frame (crop)"), ("fit", "Black bars")):
            self.c_frame.addItem(t, k)
        self.c_frame.currentIndexChanged.connect(self.frame_ui_changed)
        l.addWidget(self.c_frame)
        self.s_zoom, _ = self.slider_row(l, "Zoom", 100, 300, 100, lambda v: f"{v}%")
        self.s_zoom.valueChanged.connect(self.frame_ui_changed)
        self.s_pos, self.pos_lbls = self.slider_row(
            l, "Position", 0, 100, 50, lambda v: "left / top" if v < 45 else "right / bottom" if v > 55 else "center")
        self.s_pos.valueChanged.connect(self.frame_ui_changed)
        l = self.section("Speed")
        l.addWidget(QLabel("Video speed"))
        self.c_speed = QComboBox(); self.c_speed.setFocusPolicy(Qt.NoFocus)
        # every 10% from normal speed up to 4×, then whole steps up to 8×
        for v in [round(1 + i / 10, 1) for i in range(31)] + [5, 6, 7, 8]:
            self.c_speed.addItem("Normal speed" if v == 1 else f"{v:g}× faster", v)
        self.c_speed.currentIndexChanged.connect(self.speed_changed)
        l.addWidget(self.c_speed)
        self.l_vsound = QLabel("Video's own sound"); l.addWidget(self.l_vsound)
        self.c_vsound = QComboBox(); self.c_vsound.setFocusPolicy(Qt.NoFocus)
        for k, t in (("fast", "Sped up with the video (stays in sync)"),
                     ("normal", "Normal speed (drifts out of sync)"), ("mute", "Muted")):
            self.c_vsound.addItem(t, k)
        self.c_vsound.currentIndexChanged.connect(self.speed_changed)
        l.addWidget(self.c_vsound)
        l.addWidget(QLabel("Sound speed (songs and other audio you add)"))
        self.c_mspeed = QComboBox(); self.c_mspeed.setFocusPolicy(Qt.NoFocus)
        for v in [round(0.5 + i / 10, 1) for i in range(36)]:      # 0.5× … 4.0× in 10% steps
            self.c_mspeed.addItem("Normal speed" if v == 1 else f"{v:g}× {'faster' if v > 1 else 'slower'}", v)
        self.c_mspeed.setCurrentIndex(self.c_mspeed.findData(1))
        self.c_mspeed.currentIndexChanged.connect(self.music_speed_changed)
        l.addWidget(self.c_mspeed)
        self.l_speed_hint = self.dim("Video speed speeds up the video together with its own sound. "
                                     "Sound speed only changes the music and audio you added. Songs keep their pitch.")
        l.addWidget(self.l_speed_hint)
        for w in (self.l_vsound, self.c_vsound):
            w.hide()

    def build_sound_card(self):
        l = self.section("Sound")
        self.s_vvol, _ = self.slider_row(l, "Video sound", 0, 150, 100, lambda v: f"{v}%")
        self.s_vvol.valueChanged.connect(self.sound_ui_changed)
        self.l_music = QLabel(); self.l_music.setObjectName("dim"); self.l_music.setWordWrap(True)
        l.addSpacing(6); l.addWidget(self.l_music)
        self.sound_ui_changed()

    def show_help(self):
        howto = ("<b>How to</b><br>"
                 "1. <b>Cut:</b> drag on the blue timeline to select a part, then press <b>X</b>.<br>"
                 "2. <b>Blur (optional):</b> tick <b>Blur tools</b> under the video, then drag a box on it. Its purple bar sets when it shows.<br>"
                 "3. <b>Music:</b> click 🎵 Add music, or drop songs on the window. Each song gets a "
                 "block on the Music track.<br>"
                 "4. <b>Done:</b> click Export for YouTube.<br><br>"
                 "<b>Recommended</b> export tries your graphics card first: AV1, then HEVC, then H.264. "
                 "If none work, it uses the processor in the same order. "
                 "AV1 and HEVC make smaller files; H.264 plays everywhere.")
        rows = (("Space", "Play / pause"), ("← →", "Back / forward 1 s (Shift: 5 s)"),
                (", .", "One frame back / forward"), ("PgUp PgDn", "Back / forward 1 minute"),
                ("Mouse wheel", "Zoom the timeline (Shift: scroll)"), ("+ −", "Zoom the timeline"),
                ("I / O", "Selection starts / ends here"), ("X", "Cut out the selection"),
                ("Del", "Remove the picked cut, blur or song"), ("Right-click", "Remove things on the timeline"),
                ("Ctrl+Z / Ctrl+Y", "Undo / redo"), ("Esc", "Clear selection"),
                ("Ctrl+O / Ctrl+E", "Open / export"))
        table = "<table cellspacing=4>" + "".join(
            f"<tr><td><b>{k}</b></td><td>&nbsp;&nbsp;{v}</td></tr>" for k, v in rows) + "</table>"
        html = howto + "<br><br><b>Shortcuts</b>" + table
        html += f"<br><br><span style='color:{C_DIM}'>Kingshot Editor version {APP_VERSION}<br>Log of what happened: {LOG_FILE}</span>"
        box = QMessageBox(QMessageBox.Information, "How to use Kingshot Editor", html, QMessageBox.Ok, self)
        b = box.addButton("📁 Open log folder", QMessageBox.ActionRole)
        bt = box.addButton("🧪 Speed test", QMessageBox.ActionRole)
        bu = box.addButton("🔄 Check for updates", QMessageBox.ActionRole)
        bc = box.button(QMessageBox.Ok)
        bc.setText("Close")
        box.setEscapeButton(bc)   # without this the window's X (and Esc) do nothing
        box.exec()
        if box.clickedButton() is bu:
            self._upd_manual = True
            self.toast("Checking for updates…", 1500)
            self.updater.check()
        elif box.clickedButton() is b:
            open_folder(LOG_DIR)
        elif box.clickedButton() is bt:
            if not self.p.src:
                QMessageBox.information(self, "Speed test", "Open a video first, then run the speed test.")
            else:
                BenchDialog(self, self.p).exec()

    # ---- updates (latest GitHub release of UPDATE_REPO)
    def on_update_checked(self, upd, err):
        manual, self._upd_manual = self._upd_manual, False
        if err:
            if manual:
                QMessageBox.information(self, "Updates", "Couldn't reach the update server. Check your internet and try again.")
            return
        if not upd:
            if manual:
                QMessageBox.information(self, "Updates", f"You're up to date (version {APP_VERSION}).")
            return
        if not manual and str(self.settings.value("skipUpdate", "")) == upd["version"]:
            return
        notes = f"<br><br>{upd['notes'][:600]}".replace("\n", "<br>") if upd["notes"] else ""
        if upd["installer_needed"]:
            box = QMessageBox(QMessageBox.Information, "Update available",
                              f"Version <b>{upd['version']}</b> is available (you have {APP_VERSION}). "
                              f"This one needs the full installer.{notes}", QMessageBox.Close, self)
            g = box.addButton("Open download page", QMessageBox.AcceptRole)
            box.exec()
            if box.clickedButton() is g and upd["page"]:
                QDesktopServices.openUrl(QUrl(upd["page"]))
            return
        box = QMessageBox(QMessageBox.Question, "Update available",
                          f"Version <b>{upd['version']}</b> is available (you have {APP_VERSION}).{notes}"
                          "<br><br>Update now? The editor restarts afterwards, so finish any export first "
                          "(an open video and its cuts are not saved).", QMessageBox.NoButton, self)
        yes = box.addButton("Update now", QMessageBox.AcceptRole)
        box.addButton("Later", QMessageBox.RejectRole)
        skip = box.addButton("Skip this version", QMessageBox.DestructiveRole)
        box.exec()
        if box.clickedButton() is yes:
            self._upd = upd
            self.toast("Downloading the update…", 6000)
            self.updater.apply(upd)
        elif box.clickedButton() is skip:
            self.settings.setValue("skipUpdate", upd["version"])

    def on_update_applied(self, ok, err):
        if not ok:
            QMessageBox.warning(self, "Update failed", f"The update didn't install, nothing was changed.\n\n{err}")
            return
        box = QMessageBox(QMessageBox.Information, "Updated",
                          f"Version {self._upd['version']} is installed. Restart the editor now to use it?",
                          QMessageBox.NoButton, self)
        r = box.addButton("Restart now", QMessageBox.AcceptRole)
        box.addButton("Later", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is r:
            try:
                subprocess.Popen([sys.executable, str(Path(__file__).resolve())], cwd=str(APP_DIR), creationflags=NO_WINDOW)
            except Exception as e:  # noqa: BLE001
                log(f"restart failed: {e}")
                return
            self.close()
            QApplication.quit()

    def shortcuts(self):
        def sc(key, fn):
            s = QShortcut(QKeySequence(key), self); s.setContext(Qt.ApplicationShortcut); s.activated.connect(fn)
        sc("Space", self.toggle_play)
        sc("Left", lambda: self.step(-1)); sc("Right", lambda: self.step(1))
        sc("Shift+Left", lambda: self.step(-5)); sc("Shift+Right", lambda: self.step(5))
        sc(",", lambda: self.step(-1 / 30)); sc(".", lambda: self.step(1 / 30))
        sc("PgUp", lambda: self.step(-60)); sc("PgDown", lambda: self.step(60))
        sc("=", lambda: self.tl.zoom_by(2, self.cur_time())); sc("+", lambda: self.tl.zoom_by(2, self.cur_time()))
        sc("-", lambda: self.tl.zoom_by(0.5))
        sc("Home", lambda: self.seek(0)); sc("End", lambda: self.seek(self.p.dur))
        sc("I", lambda: self.sel_edge("a")); sc("O", lambda: self.sel_edge("b"))
        sc("X", self.cut_selection); sc("Delete", self.delete_active); sc("Backspace", self.delete_active)
        sc("Escape", self.clear_sel)
        sc("Ctrl+Z", self.undo); sc("Ctrl+Y", self.redo); sc("Ctrl+Shift+Z", self.redo)
        sc("Ctrl+O", self.open_video); sc("Ctrl+E", self.export)
        sc("Ctrl+V", self.paste)

    # ---- state
    def push_undo(self):
        log(f"edit: {sys._getframe(1).f_code.co_name} | before: cuts={len(self.p.cuts)} blurs={len(self.p.blurs)} "
            f"songs={len(self.p.musics)} speed={self.p.speed:g} mspeed={self.p.mspeed:g}")
        self.undo_stack.append(self.p.snapshot())
        del self.undo_stack[:-200]
        self.redo_stack.clear()

    def undo(self):
        if self.undo_stack:
            log("edit: undo")
            self.redo_stack.append(self.p.snapshot()); self.p.restore(self.undo_stack.pop()); self.changed()

    def redo(self):
        if self.redo_stack:
            log("edit: redo")
            self.undo_stack.append(self.p.snapshot()); self.p.restore(self.redo_stack.pop()); self.changed()

    def changed(self, light=False):
        self.view.update(); self.tl.update()
        if light:
            return
        i = self.c_mspeed.findData(self.p.mspeed)
        if i >= 0 and i != self.c_mspeed.currentIndex():
            self.c_mspeed.blockSignals(True); self.c_mspeed.setCurrentIndex(i); self.c_mspeed.blockSignals(False)
        self.render_ctx()
        loaded = bool(self.p.src)
        for b in (self.b_music, self.b_export):
            b.setEnabled(loaded)
        n = len(self.p.musics)
        self.l_music.setText("" if not n else f"{n} song{'s' * (n > 1)} on the Music track. Drag to move, drag the "
                             "ends to trim, right-click to remove.")
        self.b_undo.setEnabled(bool(self.undo_stack)); self.b_redo.setEnabled(bool(self.redo_stack))
        if loaded:
            d = self.p.fin_dur()
            warn = self.p.format == "short" and d > SHORTS_MAX
            self.l_len.setText(f"Final length {fmt(d)}" + ("  ·  over 3 min: too long for a Short, switch Format to "
                                                            "YouTube video" if warn else ""))
            self.l_len.setStyleSheet(f"color:{'#ffb86b' if warn else C_DIM}; margin-right:8px")

    def render_ctx(self):
        while self.ctx_holder.count():
            w = self.ctx_holder.takeAt(0).widget()
            if w:
                w.deleteLater()
        p = self.p
        if not p.src:
            return
        it = p.active_item()
        if it and p.active[0] == "music":
            f, l = card("Song")
            nm = ElidedLabel(it["name"]); nm.setObjectName("mname"); l.addWidget(nm)
            l.addWidget(self.dim(f"Plays at {fmt(it['start'])} → {fmt(p.music_end(it))} of the final video"))
            if it.get("warn"):
                w = QLabel("\u26a0 " + it["warn"]); w.setWordWrap(True); w.setStyleSheet("color:#ffb86b")
                l.addWidget(w)
            s, _ = self.slider_row(l, "Volume", 0, 150, int(it["vol"] * 100), lambda v: f"{v}%")
            s.sliderPressed.connect(self.push_undo)
            s.valueChanged.connect(lambda v, it=it: it.__setitem__("vol", v / 100))
            for key, text in (("fade", "Fade in / out"), ("rep", "Repeat the song to the end of the video")):
                k = QCheckBox(text); k.setChecked(it[key]); k.setFocusPolicy(Qt.NoFocus)
                k.toggled.connect(lambda on, key=key, it=it: self.music_flag(it, key, on))
                l.addWidget(k)
            row = QHBoxLayout()
            row.addWidget(self.small_btn("Move to playhead", lambda: self.music_to_playhead(it)))
            row.addWidget(self.small_btn("Remove", lambda: self.remove_item("music", it["id"]), danger=True))
            l.addLayout(row)
        elif it and p.active[0] == "blur" and self.blur_tools:
            f, l = card("Blur area")
            l.addWidget(self.big(f"{fmt(it['a'])} → {fmt(it['b'])}"))
            l.addWidget(self.dim("Drag the box on the video to move or resize it. Drag the ends of its purple bar "
                                 "to change when it shows."))
            s, _ = self.slider_row(l, "Strength", 1, 10, it["s"], str)
            s.sliderPressed.connect(self.push_undo)

            def st(v, it=it):
                it["s"] = self.p.strength = v; self.view.update()
            s.valueChanged.connect(st)
            row = QHBoxLayout()
            for text, fn in (("Whole video", lambda: self.blur_range(it, 0, p.dur)),
                             ("Start here", lambda: self.blur_range(it, min(self.cur_time(), it["b"] - .05), it["b"])),
                             ("End here", lambda: self.blur_range(it, it["a"], max(self.cur_time(), it["a"] + .05)))):
                row.addWidget(self.small_btn(text, fn))
            l.addLayout(row)
            l.addWidget(self.small_btn("Delete blur", lambda: self.remove_item("blur", it["id"]), danger=True))
        elif it and p.active[0] == "cut":
            f, l = card("Cut part")
            l.addWidget(self.big(f"{fmt(it['a'])} → {fmt(it['b'])}"))
            l.addWidget(self.dim(f"{it['b'] - it['a']:.1f}s removed from the final video. Drag the red block's ends "
                                 "to adjust."))
            l.addWidget(self.small_btn("↩ Put this part back", lambda: self.remove_item("cut", it["id"])))
        elif p.sel:
            f, l = card("Selection")
            l.addWidget(self.big(f"{fmt(p.sel[0])} → {fmt(p.sel[1])}  ({p.sel[1] - p.sel[0]:.1f}s)"))
            row = QHBoxLayout()
            b = self.small_btn("✂ Cut out  (X)", self.cut_selection); b.setObjectName("primary")
            row.addWidget(b); row.addWidget(self.small_btn("Clear", self.clear_sel)); l.addLayout(row)
            if self.blur_tools:
                l.addWidget(self.dim("Or drag a box on the video to blur an area only during this part."))
        else:
            f = None     # nothing selected: side panel stays empty, see the "?" button for How To
        if f:
            self.ctx_holder.addWidget(f)

    def big(self, t):
        l = QLabel(t); l.setStyleSheet("font-size:15px;font-weight:600"); return l

    def dim(self, t):
        l = QLabel(t); l.setWordWrap(True); l.setStyleSheet(f"color:{C_DIM}"); return l

    def small_btn(self, text, fn, danger=False):
        b = QPushButton(text); b.clicked.connect(fn); b.setFocusPolicy(Qt.NoFocus)
        if danger:
            b.setObjectName("danger")
        return b

    def blur_range(self, it, a, b):
        self.push_undo(); it["a"], it["b"] = a, b; self.changed()

    def frame_ui_changed(self):
        p = self.p
        old = p.format
        p.format = self.c_fmt.currentData() or "short"
        p.frame = self.c_frame.currentData()
        if p.format == SOURCE_FORMAT:          # exact source size: the video fills the frame 1:1
            p.frame = "fill"
            if self.c_frame.currentData() != "fill":
                self.c_frame.blockSignals(True)
                self.c_frame.setCurrentIndex(self.c_frame.findData("fill"))
                self.c_frame.blockSignals(False)
        p.pos = self.s_pos.value() / 100
        p.zoom = self.s_zoom.value() / 100
        br = p.target_bitrate()
        fps = p.info.get("fps")
        self.l_res.setText(f"Exports at {p.W}×{p.H}" + (f" · {fps:.4g} fps" if fps else "") +
                           (f" · {br / 1e6:.1f} Mbit/s like the original" if br else ""))
        show = p.frame == "fill" or p.zoom > 1
        for w in (self.s_pos, *self.pos_lbls):
            w.setVisible(show)
        self.view.update()
        if old != p.format:
            self.changed()

    def codec_changed(self):
        self.p.codec = self.c_codec.currentData() or "auto"
        self.p.quality = self.c_quality.currentData() or "same"
        self.settings.setValue("codec2", self.p.codec)
        self.settings.setValue("quality", self.p.quality)
        self.frame_ui_changed()

    def speed_changed(self):
        p = self.p
        p.speed = float(self.c_speed.currentData() or 1)
        p.vsound = "fast"      # 2.6.7: video speed always speeds up the video's own sound with it; no separate option
        self.apply_playback_rate()
        self.sound_ui_changed()
        self.changed()

    def music_speed_changed(self):
        """Music speed is independent of the video speed. Song blocks keep their start; their length on the
        timeline changes because the same part of the song now takes more or less time."""
        p = self.p
        new = float(self.c_mspeed.currentData() or 1)
        if new == p.mspeed:
            return
        self.push_undo()
        k = p.mspeed / new
        for m in p.musics:
            m["len"] = max(0.5, m["len"] * k)
            if not m["rep"]:
                m["len"] = min(m["len"], (m["sdur"] - m["inn"]) / new)
        p.mspeed = new
        self.apply_playback_rate()
        self.changed()

    def preview_speed_changed(self):
        """'Watch Nx': scrub through the preview faster without changing the exported video's speed."""
        self.preview_mult = float(self.c_preview.currentData() or 1)
        self.apply_playback_rate()

    def apply_playback_rate(self):
        self.player.setPlaybackRate(min(8.0, self.p.speed * self.preview_mult))
        for mp, _ in self.mplayers.values():
            mp.setPlaybackRate(min(8.0, self.p.mspeed * self.preview_mult))

    def video_sound_mode(self):
        """How the preview plays the video's own sound: 'own' (from the video player) / 'extra' / 'mute'."""
        p = self.p
        if p.speed == 1 or p.vsound == "fast":
            return "own"
        return "extra" if p.vsound == "normal" else "mute"

    def sync_vaudio(self, f, playing):
        """Normal-speed video sound while the picture runs fast: a second, sound-only player."""
        p = self.p
        if self.video_sound_mode() != "extra" or not p.info.get("has_audio"):
            if self.vaplayer and self.vaplayer[0].playbackState() == QMediaPlayer.PlayingState:
                self.vaplayer[0].pause()
            return
        if not self.vaplayer or self.vaplayer[2] != p.src:
            if self.vaplayer:
                self.vaplayer[0].stop(); self.vaplayer[0].deleteLater(); self.vaplayer[1].deleteLater()
            mp = QMediaPlayer(self); ao = QAudioOutput(self); mp.setAudioOutput(ao)
            mp.setSource(QUrl.fromLocalFile(p.preview or p.src))
            self.vaplayer = (mp, ao, p.src)
        mp, ao, _ = self.vaplayer
        ao.setVolume(min(1.0, p.vvol))
        pos = p.from_out(f) if playing and f < p.fin_dur() else None
        if pos is None:
            if mp.playbackState() == QMediaPlayer.PlayingState:
                mp.pause()
            return
        if mp.playbackState() != QMediaPlayer.PlayingState:
            mp.setPosition(int(pos * 1000)); mp.play()
        elif abs(mp.position() / 1000 - pos) > 0.35:
            mp.setPosition(int(pos * 1000))

    def sound_ui_changed(self):
        self.p.vvol = self.s_vvol.value() / 100
        self.aout.setVolume(min(1.0, self.p.vvol))
        self.aout.setMuted(self.video_sound_mode() != "own" and not self._await_first)

    def music_flag(self, it, key, on):
        self.push_undo()
        it[key] = on
        if key == "rep" and on:         # looping: stretch the block from its start to the end of the video
            it["len"] = max(0.5, self.p.fin_dur() - it["start"])
        if key == "rep" and not on:
            it["len"] = min(it["len"], (it["sdur"] - it["inn"]) / self.p.mspeed)
        self.changed()

    def music_to_playhead(self, it):
        self.push_undo()
        it["start"] = self.p.fin(self.cur_time())
        self.changed()

    # ---- files
    def last_dir(self):
        return self.settings.value("lastDir", str(Path.home() / "Videos"))

    def open_video(self):
        f, _ = QFileDialog.getOpenFileName(self, "Open video", self.last_dir(),
                                           "Videos (*.mp4 *.mov *.mkv *.m4v *.webm *.avi *.hevc *.av1 *.ivf *.ts *.mts *.m2ts);;All files (*)")
        if f:
            self.load_video(f)

    def show_music_menu(self):
        if not self.p.src:
            return self.toast("Open a video first.")
        menu = QMenu(self)
        menu.addAction("🎵  From your computer…", self.open_music)
        if self.yt_bg:
            menu.addAction("⬇  Show the YouTube download", self.yt_show)
        else:
            menu.addAction("▶  From a YouTube link…", lambda: self.youtube())
        menu.exec(self.b_music.mapToGlobal(self.b_music.rect().bottomLeft()))

    def open_music(self):
        if not self.p.src:
            return self.toast("Open a video first.")
        fs, _ = QFileDialog.getOpenFileNames(self, "Add songs (you can pick several)",
                                             self.settings.value("musicDir", self.last_dir()),
                                             "Audio (*.mp3 *.m4a *.wav *.ogg *.aac *.flac *.opus *.wma);;All files (*)")
        if fs:
            self.settings.setValue("musicDir", str(Path(fs[0]).parent))
            self.add_songs(fs)

    def load_video(self, path):
        try:
            info = probe(path)
        except Exception as ex:  # noqa: BLE001
            return QMessageBox.warning(self, APP_NAME, f"Can't open this file:\n{ex}")
        if not info["w"] or info["duration"] <= 0:
            return QMessageBox.warning(self, APP_NAME, "This file has no video in it.")
        self.settings.setValue("lastDir", str(Path(path).parent))
        log(f"OPEN video: {path} | {info['w']}x{info['h']} {info['duration']:.1f}s codec={info['codec']} "
            f"hdr={info['hdr']} audio={info['has_audio']} bitrate={info['vbr']}")
        self.player.stop()
        old = self.p
        p = self.p = Project()
        p.codec = self.c_codec.currentData() or "auto"
        p.quality = self.c_quality.currentData() or "same"
        self.sync_players()
        self.purge_after_export(old)
        p.src, p.info, p.dur = path, info, info["duration"]
        ar = info["w"] / info["h"]
        # pick the format from the clip: vertical and 3 min or less -> Short, longer vertical -> 9:16 video,
        # wide (or square) -> 16:9 video. Still changeable by hand under Format.
        p.format = ("short" if info["duration"] <= SHORTS_MAX else "tall") if ar < 1 else "long"
        p.frame = "fill" if abs(ar - p.W / p.H) < 0.04 else "blurbg"
        self.c_fmt.blockSignals(True); self.c_fmt.setCurrentIndex(self.c_fmt.findData(p.format)); self.c_fmt.blockSignals(False)
        self.c_frame.setCurrentIndex(self.c_frame.findData(p.frame))
        self.tl.v0, self.tl.vlen = 0.0, 0.0
        self.c_speed.blockSignals(True); self.c_speed.setCurrentIndex(0); self.c_speed.blockSignals(False)
        self.c_mspeed.blockSignals(True); self.c_mspeed.setCurrentIndex(self.c_mspeed.findData(1)); self.c_mspeed.blockSignals(False)
        self.preview_mult = 1.0
        self.c_preview.blockSignals(True); self.c_preview.setCurrentIndex(0); self.c_preview.blockSignals(False)
        self.player.setPlaybackRate(1.0)
        self.c_vsound.blockSignals(True); self.c_vsound.setCurrentIndex(0); self.c_vsound.blockSignals(False)
        for w in (self.l_vsound, self.c_vsound):
            w.hide()
        self.s_pos.setValue(50); self.s_zoom.setValue(100)
        self.frame_ui_changed(); self.sound_ui_changed()
        self.undo_stack.clear(); self.redo_stack.clear()
        self.view.img = None
        self._frame_t = 0.0
        self._first_load = True
        self.player.setSource(QUrl.fromLocalFile(path))
        clean_previews()
        self._proxy_tried = False
        if info.get("codec") == "av1":
            QTimer.singleShot(7000, lambda proj=p: self._av1_watchdog(proj))
        self.l_time.setText(f"0:00.0 / {fmt(p.dur)}")
        self.changed()
        extra = " · HEVC" if info["codec"] == "hevc" else " · AV1" if info["codec"] == "av1" else ""
        extra += " · HDR (colours get converted on export)" if info["hdr"] else ""
        self.toast(f"Opened {Path(path).name}  ({info['w']}×{info['h']}{extra})")

    def paste(self):
        txt = QApplication.clipboard().text().strip()
        if YT_RE.match(txt):
            self.youtube(txt)
        else:
            self.toast("Copy a YouTube link first, then press Ctrl+V to add its song.")

    def youtube(self, url=""):
        if not self.p.src:
            return self.toast("Open a video first.")
        if not url:
            clip = QApplication.clipboard().text().strip()
            url = clip if YT_RE.match(clip) else ""
        # the video only uses as much song as its final length (after cuts) at the music speed, plus a little spare
        limit = max(60, self.p.fin_dur() * self.p.mspeed + 15)
        d = YtDialog(self, url, limit)
        if url:
            QTimer.singleShot(0, d.go)
        r = d.exec()
        if r == YtDialog.MINIMIZED:
            self.yt_bg = d
            d.progressed.connect(self.yt_progress)
            d.finished.connect(self.yt_bg_finished)
            self.yt_bar.setValue(int(d.bar.value())); self.yt_bar.show()
            self.toast("Downloading in the background. The bar under Add music shows how much is left.")
        elif r and d.result_path:
            self.add_songs([d.result_path], warn=d.warn_text, yt=True)

    def eventFilter(self, obj, ev):
        if obj is self.b_music and ev.type() in (QEvent.Resize, QEvent.Show):
            r = self.b_music.rect()
            self.yt_bar.setGeometry(8, r.height() - 7, max(10, r.width() - 16), 4)
        return super().eventFilter(obj, ev)

    def yt_progress(self, f, text):
        if f < 0:
            self.yt_bar.hide()
        else:
            self.yt_bar.setValue(int(f * 1000)); self.yt_bar.setToolTip(text)

    def yt_show(self):
        d = self.yt_bg
        if d:
            d.minimized = False
            d.show(); d.raise_(); d.activateWindow()

    def yt_bg_finished(self, code):
        d, self.yt_bg = self.yt_bg, None
        self.yt_bar.hide()
        if d and code and d.result_path:
            self.add_songs([d.result_path], warn=d.warn_text, yt=True)
            self.toast("The YouTube song was added to the Music track.")
        if d:
            d.deleteLater()

    def add_songs(self, paths, warn="", yt=False):
        """Each song becomes a block on the Music track, one after another like a playlist."""
        p = self.p
        added = []
        for path in paths:
            try:
                d = probe(path)["duration"]
            except Exception as ex:  # noqa: BLE001
                QMessageBox.warning(self, APP_NAME, f"Can't open this audio file:\n{path}\n\n{ex}")
                continue
            if d <= 0.2:
                continue
            od = p.fin_dur()
            start = max((p.music_end(m) if m["start"] + m["len"] > od else m["start"] + m["len"]
                         for m in p.musics), default=0.0)
            if start >= od - 0.5:
                start = p.fin(self.cur_time())
            if not added:
                self.push_undo()
            m = {"id": p.new_id(), "path": path, "name": Path(path).stem, "sdur": d, "start": start, "inn": 0.0,
                 "len": max(0.5, min(d / p.mspeed, od - start)) if od - start > 0.5 else d / p.mspeed, "vol": 0.6, "fade": True,
                 "rep": False, "warn": warn}
            p.musics.append(m)
            added.append(m)
            if yt:
                p.yt_files.append(path)
        if added:
            p.active = ("music", added[-1]["id"])
            self.changed()
            self.toast(f"Added {len(added)} song{'s' * (len(added) > 1)} to the Music track 🎵")

    def load_music(self, path):          # kept for drag & drop / tests
        self.add_songs([path])

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        urls = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if not urls:
            return
        audio = [f for f in urls if Path(f).suffix.lower() in (".mp3", ".m4a", ".wav", ".ogg", ".aac", ".flac",
                                                               ".opus", ".wma")]
        if audio:
            if not self.p.src:
                return self.toast("Open a video first.")
            self.add_songs(audio)
        else:
            self.load_video(urls[0])

    # ---- playback
    def cur_time(self):
        if self._seek_target is not None:
            return self._seek_target
        return self.player.position() / 1000 if self.p.src else 0.0

    def on_frame(self, frame):
        if not frame.isValid():
            return
        img = frame_to_image(frame)
        if img.isNull():
            return
        info = self.p.info
        # rotate if the decoder handed us the stored (unrotated) picture
        if info.get("w") and (img.width() > img.height()) != (info["w"] > info["h"]) and info.get("rot"):
            img = img.transformed(QTransform().rotate(-info["rot"]))
        self.view.img = img
        self.view.update()
        if self._await_first:
            self._first_frame_done()

    def _first_frame_done(self):
        if not self._await_first:
            return
        self._await_first = False
        if self.view.img is None and self.need_proxy():      # no picture arrived: Windows can't decode this video
            QTimer.singleShot(0, self.make_proxy)
            return
        self.player.pause()
        self.player.setPosition(0)
        self.aout.setMuted(self.video_sound_mode() != "own")

    def on_status(self, st):
        if st == QMediaPlayer.LoadedMedia and self._first_load:
            self._first_load = False
            # render the first frame
            self._await_first = True
            self.aout.setMuted(True)
            self.player.play()
            QTimer.singleShot(4000, self._first_frame_done)   # safety net
        elif st == QMediaPlayer.EndOfMedia:
            self.pause_music()
        elif st == QMediaPlayer.InvalidMedia:
            if self.need_proxy():
                QTimer.singleShot(0, self.make_proxy)
            else:
                self.toast("The preview can't play this file, but export may still work.")

    def on_player_error(self, err, msg):
        log(f"player error: {err} {msg}")
        if self.need_proxy():          # AV1 that this PC can't decode: no add-on needed, FFmpeg makes a preview copy
            QTimer.singleShot(0, self.make_proxy)
        elif msg:
            self.toast(f"Playback problem: {msg}")

    def _av1_watchdog(self, proj):
        """Safety net: if an AV1 video still shows no picture a few seconds after opening, make the preview copy."""
        if proj is self.p and self.view.img is None and self.need_proxy():
            self.make_proxy()

    def need_proxy(self):
        p = self.p
        return bool(p.src) and p.info.get("codec") == "av1" and not p.preview and not getattr(self, "_proxy_tried", False)

    def make_proxy(self):
        """AV1 the preview can't decode -> make a small H.264 preview copy with FFmpeg, then play that."""
        if not self.need_proxy() or not FFMPEG:
            return
        self._proxy_tried = True
        log("AV1 preview copy: making a 720p proxy")
        self.player.stop()
        d = ProxyDialog(self, self.p)
        ok = d.exec() and d.result_path
        if not self.p.src:
            return
        if ok:
            self.p.preview = d.result_path
            self.sync_players()
            self._first_load = True
            self.view.img = None
            self.player.setSource(QUrl.fromLocalFile(d.result_path))
            self.toast("Preview ready. Your export still uses the original AV1 file.")
        else:
            self.toast("No preview for this AV1 video, but you can still cut and export it.")

    def on_state(self, st):
        self.b_play.setText("❚❚" if st == QMediaPlayer.PlayingState else "▶")
        if st != QMediaPlayer.PlayingState:
            self.pause_music()

    def playing(self):
        return self.player.playbackState() == QMediaPlayer.PlayingState

    def toggle_play(self):
        p = self.p
        if not p.src:
            return
        if self.playing():
            return self.pause()
        t = self.cur_time()
        c = p.cut_at(t)
        segs = p.keep_segments()
        if not segs:
            return self.toast("Everything is cut out. Press Ctrl+Z to undo.")
        if t >= p.dur - 0.05 or (c and c["b"] >= p.dur - 0.02) or self.player.mediaStatus() == QMediaPlayer.EndOfMedia:
            self.seek(segs[0][0])
        self.player.play()

    def pause(self):
        if self.playing():
            self.player.pause()
        self.pause_music()

    def pause_music(self):
        if self.vaplayer:
            self.vaplayer[0].pause()
        for mp, _ in self.mplayers.values():
            if mp.playbackState() == QMediaPlayer.PlayingState:
                mp.pause()

    def seek(self, t):
        if not self.p.src:
            return
        t = clamp(t, 0, max(0, self.p.dur - 0.01))
        self.player.setPosition(int(t * 1000))
        self._seek_target = t
        self.tl.follow(t)
        QTimer.singleShot(120, self._clear_seek)
        self.view.update(); self.tl.update()

    def _clear_seek(self):
        self._seek_target = None

    def step(self, d):
        self.pause(); self.seek(self.cur_time() + d)

    def tick(self):
        p = self.p
        if not p.src:
            return
        t = self.cur_time()
        if self.playing():
            c = p.cut_at(t)
            if c:
                if c["b"] >= p.dur - 0.02:
                    self.pause()
                else:
                    self.seek(c["b"]); t = c["b"]
            self.tl.follow(t)
            self.tl.update()
        self.l_time.setText(f"{fmt(t)} / {fmt(p.dur)}")
        self.sync_music(p.fin(t), self.playing())
        self.sync_vaudio(p.fin(t), self.playing())

    def sync_players(self):
        """One media player per song block; drop players of removed blocks."""
        ids = {m["id"] for m in self.p.musics}
        for i in list(self.mplayers):
            if i not in ids:
                mp, ao = self.mplayers.pop(i)
                mp.stop(); mp.setSource(QUrl()); mp.deleteLater(); ao.deleteLater()
        for m in self.p.musics:
            if m["id"] not in self.mplayers:
                mp = QMediaPlayer(self); ao = QAudioOutput(self); mp.setAudioOutput(ao)
                mp.setSource(QUrl.fromLocalFile(m["path"]))
                mp.setPlaybackRate(min(8.0, self.p.mspeed * self.preview_mult))
                self.mplayers[m["id"]] = (mp, ao)

    def sync_music(self, o, playing):
        p = self.p
        if len(self.mplayers) != len(p.musics):
            self.sync_players()
        for m in p.musics:
            if m["id"] not in self.mplayers:
                continue
            mp, ao = self.mplayers[m["id"]]
            pos = p.clip_pos(m, o, p.mspeed) if playing and o < p.fin_dur() else None
            if pos is None:
                if mp.playbackState() == QMediaPlayer.PlayingState:
                    mp.pause()
                continue
            ao.setVolume(min(1.0, p.clip_gain(m, o)))
            if mp.playbackState() != QMediaPlayer.PlayingState:
                mp.setPosition(int(pos * 1000)); mp.play()
            elif abs(mp.position() / 1000 - pos) > 0.35:
                mp.setPosition(int(pos * 1000))

    # ---- edits
    def sel_edge(self, which):
        p = self.p
        if not p.src:
            return
        t = self.cur_time()
        if which == "a":
            p.sel = (t, p.sel[1] if p.sel and p.sel[1] > t + .05 else p.dur)
        else:
            p.sel = (p.sel[0] if p.sel and p.sel[0] < t - .05 else 0, t)
        p.active = None
        self.changed()

    def cut_selection(self):
        p = self.p
        if not p.src:
            return
        if not p.sel:
            return self.toast("First drag on the timeline to select the part you want to remove.")
        self.push_undo()
        p.cuts.append({"id": p.new_id(), "a": p.sel[0], "b": p.sel[1]})
        p.sel = None
        p.norm_cuts()
        self.changed()
        self.toast("Careful: you've cut out the whole video. Ctrl+Z to undo." if not p.keep_segments()
                   else "Part cut out ✂  (Ctrl+Z to undo)")

    def remove_item(self, kind, id_):
        self.push_undo()
        p = self.p
        if kind == "cut":
            p.cuts = [c for c in p.cuts if c["id"] != id_]
        elif kind == "music":
            p.musics = [m for m in p.musics if m["id"] != id_]
            self.sync_players()
        else:
            p.blurs = [b for b in p.blurs if b["id"] != id_]
        if p.active and p.active[1] == id_:
            p.active = None
        self.changed()

    def delete_active(self):
        if self.p.active:
            self.remove_item(*self.p.active)

    def clear_sel(self):
        self.p.sel = None; self.p.active = None; self.changed()

    # ---- export
    def export(self):
        p = self.p
        if not p.src:
            return
        if not FFMPEG:
            return QMessageBox.warning(self, APP_NAME, "FFmpeg is missing. Please run Setup again.")
        if not p.keep_segments():
            return self.toast("Nothing left to export: the whole video is cut out.")
        self.pause()
        src = Path(p.src)
        # exports always start in Videos\YT Ready (created on the fly), every time
        out_dir = yt_ready_dir()
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            out_dir = src.parent
        default = str(out_dir / f"{src.stem}_{'short' if p.format == 'short' else 'youtube'}.mp4")
        out, _ = QFileDialog.getSaveFileName(self, "Save video as", default, "MP4 video (*.mp4)")
        if not out:
            return
        if not out.lower().endswith(".mp4"):
            out += ".mp4"
        if Path(out).resolve() == src.resolve():
            return QMessageBox.warning(self, APP_NAME, "Please pick a different name than the original video.")
        self.toast("Checking your graphics card…", 1500)
        QApplication.processEvents()
        ExportDialog(self, p, out, export_plan(p.codec)).exec()

    # ---- toast
    def toast(self, msg, ms=3200):
        t = self.toast_lbl
        t.setText(msg); t.adjustSize()
        t.move((self.width() - t.width()) // 2, self.height() - 190)
        t.show(); t.raise_()
        self.toast_timer.start(ms)

    def purge_after_export(self, proj):
        """Once a video has been exported, the songs downloaded for it are no longer needed: delete them when the
        project is closed (a new video is opened, or the app is closed), so re-exporting still works until then."""
        if not (proj.exported and proj.yt_files):
            return
        files = list(proj.yt_files)
        proj.yt_files = []
        left = purge_song_files(files)
        if left:     # Windows may still hold the file for a moment: try once more shortly
            QTimer.singleShot(2000, lambda: purge_song_files(left))

    def closeEvent(self, e):
        if self.yt_bg:
            self.yt_bg.cancel()
        self.player.stop(); self.pause_music()
        proj = self.p
        self.p = Project()
        self.sync_players()          # let go of the song files so they can be deleted
        self.purge_after_export(proj)
        clean_download_leftovers(0)
        clean_previews()
        super().closeEvent(e)


STYLE = f"""
QWidget {{ color:{C_TEXT}; font-size:13px; }}
QMainWindow, QWidget#sideInner {{ background:{C_PANEL}; }}
QToolBar {{ background:{C_PANEL}; border:0; border-bottom:1px solid {C_LINE}; padding:6px 8px; spacing:6px; }}
QLabel#brand {{ font-size:16px; font-weight:700; }}
QLabel#time {{ font-size:14px; font-weight:600; padding-left:6px; }}
QPushButton {{ background:{C_PANEL2}; border:1px solid {C_LINE}; border-radius:7px; padding:6px 12px; }}
QPushButton:hover {{ border-color:#3d4560; background:#252b3a; }}
QPushButton:disabled {{ color:#555c6e; }}
QPushButton#primary {{ background:{C_ACCENT}; border-color:{C_ACCENT}; color:white; font-weight:600; }}
QPushButton#primary:hover {{ background:#3a72e0; }}
QPushButton#danger {{ color:#ffb4b6; border-color:#5a2a30; }}
QFrame#card {{ background:{C_PANEL2}; border:1px solid #33405c; border-radius:10px; }}
QFrame#section {{ border:0; border-top:1px solid {C_LINE}; }}
QLabel#cardTitle {{ color:{C_DIM}; font-size:11px; font-weight:700; letter-spacing:1px; }}
QLabel#val {{ color:{C_TEXT}; font-weight:600; }}
QLabel#dim {{ color:{C_DIM}; font-size:12px; }}
QLabel#mname {{ color:#34d399; font-weight:600; }}
QFrame#bottom {{ background:{C_PANEL}; border-top:1px solid {C_LINE}; }}
QScrollArea#side {{ border:0; border-left:1px solid {C_LINE}; background:{C_PANEL}; }}
QComboBox {{ background:{C_BG}; border:1px solid {C_LINE}; border-radius:7px; padding:6px 8px; }}
QComboBox:hover {{ border-color:#3d4560; }}
QComboBox::drop-down {{ border:0; width:22px; }}
QComboBox QAbstractItemView {{ background:{C_PANEL2}; border:1px solid {C_LINE}; selection-background-color:{C_ACCENT}; }}
QCheckBox {{ spacing:8px; }}
QSlider {{ min-height:18px; }}
QSlider::groove:horizontal {{ height:4px; background:{C_LINE}; border-radius:2px; }}
QSlider::sub-page:horizontal {{ background:{C_ACCENT}; border-radius:2px; }}
QSlider::handle:horizontal {{ background:white; width:14px; height:14px; margin:-5px 0; border-radius:7px; }}
QScrollBar:horizontal {{ background:{C_PANEL}; height:10px; margin:0; }}
QScrollBar::handle:horizontal {{ background:#3a4258; border-radius:5px; min-width:30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width:0; height:0; }}
QScrollBar:vertical {{ background:{C_PANEL}; width:8px; }}
QScrollBar::handle:vertical {{ background:#3a4258; border-radius:4px; min-height:30px; }}
QMenu {{ background:{C_PANEL2}; border:1px solid {C_LINE}; padding:4px; }}
QMenu::item {{ padding:6px 18px; border-radius:5px; }}
QMenu::item:selected {{ background:{C_ACCENT}; }}
QLabel#toast {{ background:#262d3d; border:1px solid #3a4258; border-radius:8px; padding:9px 16px; }}
QProgressBar {{ background:{C_BG}; border:0; border-radius:5px; height:10px; }}
QProgressBar::chunk {{ background:{C_ACCENT}; border-radius:5px; }}
QDialog, QMessageBox {{ background:{C_PANEL2}; }}
"""


def main():
    if os.name == "nt":
        try:   # own taskbar icon instead of Python's
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Kingshot.Editor")
        except Exception:  # noqa: BLE001
            pass
    sys.excepthook = _log_crash
    log(f"==== {APP_NAME} {APP_VERSION} started | ffmpeg={FFMPEG} | python {sys.version.split()[0]} | {sys.platform}")
    app = QApplication(sys.argv)
    setup_app(app)
    w = Main()
    w.show()
    if len(sys.argv) > 1 and Path(sys.argv[1]).exists():
        w.load_video(sys.argv[1])
    sys.exit(app.exec())


def setup_app(app):
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    pal = app.palette()
    for role, col in ((QPalette.Window, C_PANEL), (QPalette.Base, C_BG), (QPalette.Button, C_PANEL2),
                      (QPalette.Text, C_TEXT), (QPalette.WindowText, C_TEXT), (QPalette.ButtonText, C_TEXT),
                      (QPalette.Highlight, C_ACCENT)):
        pal.setColor(role, QColor(col))
    app.setPalette(pal)
    app.setStyleSheet(STYLE)
    ico = APP_DIR / "icon.ico"
    if ico.exists():
        app.setWindowIcon(QIcon(str(ico)))


if __name__ == "__main__":
    main()
