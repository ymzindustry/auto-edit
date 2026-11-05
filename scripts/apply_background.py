#!/home/ymz/.hermes/profiles/videoeditor/venv_video/bin/python
"""
apply_background.py — Put a clip's subject on a new background.

  --background green                  -> solid chroma-key green (#00FF00)
  --background /path/img.jpg          -> a still image
  --background /path/bg.mp4           -> a video (looped; --offset keeps it continuous)

The subject is cut out with AI segmentation (--model u2net / u2netp / ...) or,
with --model without, with a fast chroma key on the clip's own backdrop.

--offset (seconds) seeks the background video so that, when all the segments are
played back-to-back, the background keeps flowing without jumping.

Usage:
    apply_background.py seg.mp4 -o out.mp4 --background green --model u2netp
    apply_background.py seg.mp4 -o out.mp4 --background bg.mp4 --offset 12.5
"""

import os
import sys

# Self-heal: if this script was started with another interpreter (the very common
# `python3 apply_background.py`), restart it under our own venv so imports resolve.
_VENV_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "venv_video")
_VENV_PY = os.path.join(_VENV_DIR, "bin", "python")
if (os.environ.get("_WK_REEXEC") != "1" and os.path.exists(_VENV_PY)
        and os.path.abspath(sys.prefix) != os.path.abspath(_VENV_DIR)):
    os.environ["_WK_REEXEC"] = "1"
    os.execv(_VENV_PY, [_VENV_PY, os.path.abspath(__file__), *sys.argv[1:]])

import argparse
import subprocess
import time

import numpy as np
import cv2
import imageio_ffmpeg

# rembg downloads its ONNX models into U2NET_HOME (default ~/.u2net, which is OUTSIDE
# this profile): pin it to models/rembg so every model stays inside the profile.
_PROFILE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("U2NET_HOME", os.path.join(_PROFILE, "models", "rembg"))
os.makedirs(os.environ["U2NET_HOME"], exist_ok=True)

from rembg import new_session, remove   # noqa: E402  (must come after U2NET_HOME)
from PIL import Image                   # noqa: E402

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
GREEN_BGR = (0, 255, 0)
VIDEO_EXT = (".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v")


def _fit_dims(w: int, h: int, max_size) -> tuple:
    """Working size for the segmentation: fits inside max_size ('WxH'), aspect kept.

    This box has 3.8 GB of RAM and a 4K frame is 8.3 MP: running a rembg model on it
    gets the process killed (SIGKILL), which is why every AI path works at 1920x1080 by
    default and only the frame fed to the model is shrunk — the composite, the
    background and the encoder all use the same working size.

    The box is ORIENTATION-AWARE (user's rule: "si portrait alors garde portrait"): a
    portrait clip is compared the other way round, so 1080x2400 with 1920x1080 gives
    864x1920 (portrait) and not 486x1080. The aspect ratio is never changed.
    """
    if not max_size:
        return w, h
    mw, mh = max_size
    if h > w:                       # portrait: swap the box so it stays portrait
        mw, mh = mh, mw
    if w <= mw and h <= mh:
        return w, h
    s = min(mw / w, mh / h)
    return (max(2, int(round(w * s)) // 2 * 2), max(2, int(round(h * s)) // 2 * 2))


def is_video(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in VIDEO_EXT


def _fit_frame(img: np.ndarray, w: int, h: int, fit: str = "cover") -> np.ndarray:
    """Resize a background image/frame to exactly w x h.

    cover   -> fills the frame, cropping the overflow (no distortion) [default]
    contain -> fits inside, letterboxed with black
    stretch -> plain resize (may distort)
    """
    ih, iw = img.shape[:2]
    if fit == "stretch" or (iw == w and ih == h):
        return cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)
    s = min(w / iw, h / ih) if fit == "contain" else max(w / iw, h / ih)
    nw, nh = max(1, int(iw * s + 0.5)), max(1, int(ih * s + 0.5))
    resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
    if fit == "contain":
        canvas = np.zeros((h, w, 3), np.uint8)
        x, y = (w - nw) // 2, (h - nh) // 2
        canvas[y:y + nh, x:x + nw] = resized
        return canvas
    x, y = (nw - w) // 2, (nh - h) // 2
    return resized[y:y + h, x:x + w]


def _outline_color(c: str) -> str:
    """'#FFFFFF' / 'FFFFFF' / '0xFFFFFF' -> '0xFFFFFF' (ffmpeg color syntax)."""
    c = c.strip().lstrip("#")
    if c.lower().startswith("0x"):
        c = c[2:]
    return "0x" + c.upper()


class Background:
    """Serves background frames (green / image / video) at the clip's resolution."""

    def __init__(self, background: str, w: int, h: int, offset: float = 0.0,
                 fit: str = "cover"):
        self.w, self.h = w, h
        self.fit = fit
        self.cap = None
        self.static = None
        bg = background.strip()
        if bg.lower() in ("green", "#00ff00", "00ff00"):
            self.static = np.full((h, w, 3), GREEN_BGR, np.uint8)
        elif is_video(bg):
            self.cap = cv2.VideoCapture(bg)
            fps = self.cap.get(cv2.CAP_PROP_FPS) or 30.0
            if offset > 0:
                self.cap.set(cv2.CAP_PROP_POS_FRAMES, int(round(offset * fps)))
        else:
            img = cv2.imread(bg)
            if img is None:
                raise SystemExit(f"Cannot read background image: {bg}")
            self.static = _fit_frame(img, w, h, fit)

    def get(self) -> np.ndarray:
        if self.static is not None:
            return self.static
        ok, frame = self.cap.read()
        if not ok:                                  # loop the background video
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = self.cap.read()
        if not ok:
            return np.zeros((self.h, self.w, 3), np.uint8)
        return _fit_frame(frame, self.w, self.h, self.fit)


def mux_audio(video_only: str, original: str, output_path: str) -> None:
    subprocess.run([FFMPEG, "-y", "-i", video_only, "-i", original,
                    "-map", "0:v", "-map", "1:a?",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
                    output_path], check=True, capture_output=True, text=True)


def apply_ai(video_path: str, output_path: str, model: str,
             background: str, offset: float, crf: int, fit: str = "cover",
             max_size=None) -> None:
    cap = cv2.VideoCapture(video_path)
    w0 = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h0 = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    w, h = _fit_dims(w0, h0, max_size)
    if (w, h) != (w0, h0):
        print(f"  working size {w}x{h} (input {w0}x{h0}) — the AI runs there: 4K needs "
              f"more RAM than this machine has (it was killed with SIGKILL)")
    print(f"  {w}x{h} @ {fps:.2f}fps, {total} frames, model={model}, bg={background}")
    sess = new_session(model)
    bg = Background(background, w, h, offset, fit)
    erode_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))

    out_dir = os.path.dirname(os.path.abspath(output_path)) or "."
    video_only = os.path.join(out_dir, ".ab_tmp.mp4")
    if os.path.exists(video_only):      # leftovers of a run killed mid-encode
        os.remove(video_only)
    enc = subprocess.Popen([FFMPEG, "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
                            "-s", f"{w}x{h}", "-r", str(fps), "-i", "-",
                            "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
                            "-pix_fmt", "yuv420p", video_only], stdin=subprocess.PIPE)

    t0, i = time.time(), 0
    while True:
        ok, fg = cap.read()
        if not ok:
            break
        if (w, h) != (w0, h0):
            fg = cv2.resize(fg, (w, h), interpolation=cv2.INTER_AREA)
        pil = Image.fromarray(cv2.cvtColor(fg, cv2.COLOR_BGR2RGB))
        out = remove(pil, session=sess)
        alpha = np.array(out)[..., 3].astype(np.float32)
        alpha = cv2.erode(alpha, erode_k, iterations=1)
        alpha = cv2.GaussianBlur(alpha, (5, 5), 1.0)
        a = (alpha / 255.0)[..., None]
        bgf = bg.get().astype(np.float32)
        comp = (fg.astype(np.float32) * a + bgf * (1.0 - a)).astype(np.uint8)
        enc.stdin.write(comp.tobytes())
        if i % 60 == 0:
            el = max(time.time() - t0, 1e-9)
            done = (i + 1) / el
            print(f"  frame {i}/{total} ({done:.2f} fps, ETA {(total - i - 1) / done / 60:.1f} min)",
                  flush=True)
        i += 1
    enc.stdin.close()
    enc.wait()
    cap.release()
    mux_audio(video_only, video_path, output_path)
    os.remove(video_only)


def _sample_backdrop(frame: np.ndarray) -> np.ndarray:
    h, w = frame.shape[:2]
    m = max(8, int(min(h, w) * 0.06))
    corners = np.concatenate([frame[:m, :m].reshape(-1, 3), frame[:m, -m:].reshape(-1, 3),
                              frame[-m:, :m].reshape(-1, 3), frame[-m:, -m:].reshape(-1, 3)])
    return np.median(corners, axis=0)


def apply_chroma(video_path: str, output_path: str, background: str,
                 offset: float, crf: int, tolerance: float,
                 key_color: str | None = None, fit: str = "cover",
                 spill: float = 0.5, outline: str | None = None,
                 outline_width: float = 4, max_size=None) -> None:
    cap = cv2.VideoCapture(video_path)
    w0 = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h0 = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    w, h = _fit_dims(w0, h0, max_size)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    ok, frame = cap.read()
    cap.release()
    if key_color:
        key_rgb = int(key_color.lstrip("#").replace("0x", "").replace("0X", ""), 16)
    else:
        bg_bgr = _sample_backdrop(frame)
        key_rgb = (int(bg_bgr[2]) << 16) | (int(bg_bgr[1]) << 8) | int(bg_bgr[0])
    similarity = min(1.0, max(0.01, tolerance / 255.0))
    # same working size as the AI path: the keyed chain is built at (w, h), so a big
    # input is scaled+letterboxed down to it in one pass (no-op when sizes match).
    in_scale = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,"
                if (w, h) != (w0, h0) else "")
    if (w, h) != (w0, h0):
        print(f"  working size {w}x{h} (input {w0}x{h0})")
    print(f"  chroma key 0x{key_rgb:06X} (similarity {similarity:.3f}), bg={background}")

    if background.strip().lower() in ("green", "#00ff00", "00ff00"):
        bg_in = ["-f", "lavfi", "-i", f"color=c=0x00FF00:s={w}x{h}:r={fps:.4f}"]
    elif is_video(background):
        bg_in = ["-stream_loop", "-1", "-ss", str(offset), "-i", background]
    else:
        bg_in = ["-loop", "1", "-i", background]

    if fit == "stretch":
        bg_scale = f"scale={w}:{h}"
    elif fit == "contain":
        bg_scale = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                    f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black")
    else:  # cover (default): fill the frame, crop the overflow, no distortion
        bg_scale = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"

    # Despill: kill the green halo the keyer leaves on the subject's edges
    # (ears, hair, shoulders, t-shirt). 0 disables it.
    spill_f = f",despill=type=green:mix={spill:.2f}" if spill > 0 else ""
    print(f"  despill mix={spill:.2f}" if spill > 0 else "  despill OFF")

    keyed = f"[0:v]{in_scale}colorkey=0x{key_rgb:06X}:{similarity:.3f}:0.10{spill_f}"
    bg_pipe = f"[1:v]{bg_scale},fps={fps:.4f}[bg]"

    if outline:
        # Draw a ring of `outline_width` px around the subject in --outline colour.
        #   ring = dilate(mask, width) AND NOT mask   (blend multiply vs negated mask)
        oc = _outline_color(outline)
        width = max(1, int(round(outline_width)))
        dilate = ",".join(["dilation"] * width)
        print(f"  outline {oc} width={width}px")
        fc = (
            keyed + ",format=rgba,split[s1][s2];"
            f"[s1]alphaextract,format=gray,split[m1][m2];"
            f"[m1]{dilate}[md];"
            f"[m2]negate[mn];"
            f"[md]format=gray[mdg];"
            f"[mdg][mn]blend=all_mode=multiply[ring];"
            f"color=c={oc}:s={w}x{h}:r={fps:.4f},format=rgba[cs];"
            f"[cs][ring]alphamerge[ringl];"
            f"{bg_pipe};"
            f"[bg][ringl]overlay=shortest=1[base];"
            f"[base][s2]overlay=shortest=1[out]"
        )
    else:
        fc = keyed + "[fg];" + bg_pipe + ";[bg][fg]overlay=shortest=1[out]"

    cmd = [FFMPEG, "-y", "-i", video_path, *bg_in, "-filter_complex", fc,
           "-map", "[out]", "-map", "0:a?",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", output_path]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg failed:\n" + r.stderr[-1500:])


def main() -> None:
    ap = argparse.ArgumentParser(description="Put a clip's subject on a new background.")
    ap.add_argument("input", help="Input clip (.mp4)")
    ap.add_argument("-o", "--output", required=True, help="Output clip path")
    # --background: what goes BEHIND the subject —
    #              'green' = solid chroma green (what greenscreen_workflow asks for: it is the
    #                flexible "master" you can key later in any editor),
    #              an image path = fixed backdrop,
    #              a video path = played behind the subject and ADVANCED by --offset
    #                so it flows continuously across clips instead of restarting.
    # --model    : how the subject is isolated — 'without' = fast chroma key on the
    #              clip's own backdrop (no AI, instant); otherwise any rembg model
    #              (u2netp default = fast, u2net = better/slower, u2net_human_seg = people).
    # --offset   : where to start inside the background VIDEO, in seconds. background_workflow
    #              passes the running sum of the previous segment durations so the
    #              background never jumps at a join.
    # --tolerance: chroma-key tolerance, only with --model without (60 is calibrated;
    #              raising it far enough makes the subject see-through).
    # --key      : force the colour to remove, e.g. '00FF00'. By default the backdrop
    #              colour is sampled from the clip's own first frame.
    # --bg-fit   : cover = fill+crop, aspect kept (default); contain = letterbox;
    #              stretch = distorts the background.
    # --spill    : removes the green halo left on the subject's edges (0.5 default, 0 = off).
    # --outline  : coloured ring drawn around the subject, e.g. '#FFFFFF' (default none);
    #              --outline-width = its thickness in pixels.
    # --crf      : H.264 quality of the output (18 = visually lossless, 23 = smaller).
    ap.add_argument("--background", default="green",
                    help="'green' | path to an image | path to a video")
    ap.add_argument("--model", default="u2netp",
                    help="'without' = chroma key | any rembg model (default u2netp)")
    ap.add_argument("--offset", type=float, default=0.0,
                    help="Background video start offset in seconds (keeps it continuous)")
    ap.add_argument("--tolerance", type=float, default=60.0,
                    help="Chroma-key tolerance for --model without (default 60)")
    ap.add_argument("--key", default=None,
                    help="Explicit key color for --model without (e.g. '00FF00'); "
                         "default: sample the clip's own backdrop")
    ap.add_argument("--bg-fit", default="cover", choices=["cover", "contain", "stretch"],
                    help="How the background fills the frame: cover = fill+crop (no "
                         "distortion, default), contain = letterbox, stretch = distort")
    ap.add_argument("--spill", type=float, default=0.5,
                    help="Despill strength on the subject's edges, 0 = off (default 0.5)")
    ap.add_argument("--outline", default=None,
                    help="Outline colour drawn around the subject, e.g. '#FFFFFF' "
                         "(default: no outline)")
    ap.add_argument("--outline-width", type=float, default=4,
                    help="Outline thickness in pixels (default 4)")
    ap.add_argument("--crf", type=int, default=18)
    # --max-size : WORKING resolution. This machine has 3.8 GB of RAM: a 4K frame is
    #              8.3 MP and the Apple/Meta segmentation nets need more than that, so
    #              the process is killed (SIGKILL: 9). The clip is scaled down to fit
    #              inside WxH (aspect kept) before the model runs and the output is
    #              written at that size. 'none' keeps the native size (big RAM only).
    ap.add_argument("--max-size", default="none", metavar="WxH",
                    help="Explicitly shrink the frame to fit WxH before segmenting "
                         "(default 'none' = the clip is used as it is; a 4K input WILL "
                         "be killed by the kernel — out of memory)")
    args = ap.parse_args()

    ms = str(args.max_size).strip().lower()
    max_size = None if ms in ("none", "0", "off") else tuple(
        int(v) for v in ms.replace("*", "x").split("x"))

    if args.model == "without":
        apply_chroma(args.input, args.output, args.background,
                     args.offset, args.crf, args.tolerance, args.key, args.bg_fit,
                     args.spill, args.outline, args.outline_width, max_size)
    else:
        apply_ai(args.input, args.output, args.model,
                 args.background, args.offset, args.crf, args.bg_fit, max_size)
    print(f"  -> {args.output}")


if __name__ == "__main__":
    main()
