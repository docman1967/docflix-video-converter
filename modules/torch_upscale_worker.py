#!/usr/bin/env python3
"""
Docflix Media Suite — PyTorch/CUDA AI-upscale worker (streamed engine).

Runs as a STANDALONE subprocess under a torch-capable Python interpreter
(see torch_upscaler.find_torch_python). It is intentionally free of any
relative/package imports so it can be launched by a foreign venv.

Pipeline (no PNG on disk — the whole point):

    ffmpeg decode (NVDEC)  ─►  raw rgb24 frames over a pipe
        ─►  GPU upscale (spandrel model, fp16 tensor cores)
        ─►  optional strength blend + target-height downscale (on GPU)
        ─►  raw rgb24 frames over a pipe  ─►  ffmpeg NVENC encode ─► segment file

One worker handles one GPU and one frame range [frame_start, frame_end]
(inclusive; -1 = to end). The parent (TorchUpscaleJob) fans out one worker
per card, then concats the segments and muxes the original audio/subs back in.

The process is pinned to its card via CUDA_VISIBLE_DEVICES set by the parent,
so INSIDE here everything is device 0 (torch cuda:0, ffmpeg hwaccel_device 0,
NVENC gpu 0). Emits JSON lines on stdout: {"t":"progress","frame":N} and a
final {"t":"done",...} or {"t":"error","msg":...}.

Usage:  python torch_upscale_worker.py /path/to/job.json
"""

import json
import subprocess
import sys
import time


def emit(obj):
    """Write one JSON progress/status line and flush (parent reads line-by-line)."""
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main(job_path):
    with open(job_path) as f:
        job = json.load(f)

    src        = job["input"]
    out        = job["output"]
    model_pth  = job["model_pth"]
    scale      = int(job["scale"])
    in_w       = int(job["in_w"])
    in_h       = int(job["in_h"])
    fps        = str(job["fps"])
    f_start    = int(job.get("frame_start", 0))
    f_end      = int(job.get("frame_end", -1))     # inclusive; -1 = to EOF
    batch      = max(1, int(job.get("batch", 1)))
    strength   = max(0, min(100, int(job.get("strength", 100)))) / 100.0
    target_h   = job.get("target_h")               # final height or None
    encoder    = job.get("encoder", "hevc_nvenc")
    cq         = str(job.get("cq", "18"))
    preset     = job.get("preset", "p5")
    pix_fmt    = job.get("pix_fmt", "p010le")
    expected   = job.get("expected_frames")        # for progress %, may be None

    # Heavy imports happen here so a bad/missing torch surfaces as a clean error line.
    try:
        import numpy as np
        import torch
        import torch.nn.functional as F
        from spandrel import ModelLoader, ImageModelDescriptor
    except Exception as e:  # noqa: BLE001
        emit({"t": "error", "msg": f"torch/spandrel import failed: {e}"})
        return 3

    if not torch.cuda.is_available():
        emit({"t": "error", "msg": "CUDA not available to this interpreter"})
        return 3

    try:
        model = ModelLoader().load_from_file(model_pth)
        if not isinstance(model, ImageModelDescriptor):
            emit({"t": "error", "msg": f"unsupported model type: {type(model)}"})
            return 3
        model.cuda().eval()
        model_scale = int(getattr(model, "scale", scale) or scale)
    except Exception as e:  # noqa: BLE001
        emit({"t": "error", "msg": f"model load failed: {e}"})
        return 3

    torch.backends.cudnn.benchmark = True

    # ── Output geometry ──────────────────────────────────────────────
    up_w, up_h = in_w * model_scale, in_h * model_scale
    if target_h and int(target_h) < up_h:
        out_h = int(target_h) // 2 * 2
        out_w = int(round(in_w * out_h / in_h)) // 2 * 2
    else:
        out_h, out_w = up_h, up_w
    do_downscale = (out_w, out_h) != (up_w, up_h)
    use_blend = strength < 0.999

    # ── ffmpeg decode: NVDEC, optional exact frame-range select, raw rgb24 ──
    dec_cmd = ["ffmpeg", "-v", "error", "-hwaccel", "cuda", "-hwaccel_device", "0",
               "-i", src]
    if f_start > 0 or f_end >= 0:
        end = f_end if f_end >= 0 else 2_000_000_000
        dec_cmd += ["-vf", f"select=between(n\\,{f_start}\\,{end})", "-vsync", "0"]
    dec_cmd += ["-f", "rawvideo", "-pix_fmt", "rgb24", "-"]

    # ── ffmpeg encode: raw rgb24 in → NVENC segment out ──
    enc_cmd = ["ffmpeg", "-v", "error", "-y",
               "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-s", f"{out_w}x{out_h}", "-framerate", fps, "-i", "-",
               "-c:v", encoder, "-preset", preset, "-cq", cq,
               "-pix_fmt", pix_fmt, "-f", "mpegts", out]

    # ── Model forward, with optional spatial tiling to bound VRAM ──
    # A full 4x pass on a 1080p frame peaks ~12 GB; tiling caps peak memory to a
    # single tile regardless of frame size, so a large source or a congested GPU
    # doesn't OOM. tile_state[0] == 0 means "no tiling" (fastest path); the warmup
    # below auto-picks the largest tile that fits.
    tile_state = [0]

    def _forward_tiled(x, tile, pad):
        # Run the model tile-by-tile with `pad` px of context (trimmed after) to
        # avoid visible seams, assembling the full model_scale-x output.
        b, c, h, w = x.shape
        s = model_scale
        out = torch.zeros((b, c, h * s, w * s), dtype=torch.float32, device=x.device)
        for y0 in range(0, h, tile):
            for x0 in range(0, w, tile):
                y1, x1 = min(y0 + tile, h), min(x0 + tile, w)
                ry0, rx0 = max(y0 - pad, 0), max(x0 - pad, 0)
                ry1, rx1 = min(y1 + pad, h), min(x1 + pad, w)
                with torch.autocast("cuda", dtype=torch.float16):
                    o = model(x[:, :, ry0:ry1, rx0:rx1]).float()
                ct, cl = (y0 - ry0) * s, (x0 - rx0) * s
                out[:, :, y0 * s:y1 * s, x0 * s:x1 * s] = \
                    o[:, :, ct:ct + (y1 - y0) * s, cl:cl + (x1 - x0) * s]
                del o
        return out

    def _forward(x):
        if tile_state[0] > 0:
            t = tile_state[0]
            return _forward_tiled(x, t, max(16, t // 8))
        with torch.autocast("cuda", dtype=torch.float16):
            return model(x).float()

    def upscale(batch_np):
        # (B,H,W,3) uint8 → GPU fp16 → model → (optional blend/downscale) → (B,Ho,Wo,3) uint8
        x = torch.from_numpy(batch_np).cuda().permute(0, 3, 1, 2).float().div_(255.0)
        with torch.no_grad():
            y = _forward(x)
            with torch.autocast("cuda", dtype=torch.float16):
                if use_blend:
                    base = F.interpolate(x, size=(up_h, up_w), mode="bicubic",
                                         align_corners=False)
                    y = y.mul_(strength).add_(base.mul_(1.0 - strength))
                if do_downscale:
                    y = F.interpolate(y, size=(out_h, out_w), mode="bicubic",
                                      align_corners=False)
        return (y.clamp_(0, 1).mul_(255).round_().byte()
                .permute(0, 2, 3, 1).contiguous().cpu().numpy())

    # Warmup (untimed) — pays cudnn autotune + kernel compile once, AND auto-selects
    # a tile size that fits VRAM: try full-frame first, then progressively smaller
    # tiles on CUDA OOM. Warms with the real batch so the decision matches the run.
    _OOM = getattr(torch.cuda, "OutOfMemoryError", RuntimeError)

    def _is_oom(err):
        return isinstance(err, _OOM) or "out of memory" in str(err).lower()

    warm_frame = np.zeros((batch, in_h, in_w, 3), np.uint8)
    warmed = False
    for candidate in (0, 1024, 768, 512, 384, 256):
        tile_state[0] = candidate
        try:
            torch.cuda.empty_cache()
            upscale(warm_frame)
            torch.cuda.synchronize()
            warmed = True
            if candidate:
                emit({"t": "log",
                      "msg": f"VRAM-limited: auto-tiling at {candidate}px to fit"})
            break
        except RuntimeError as e:  # OutOfMemoryError subclasses RuntimeError
            if _is_oom(e):
                torch.cuda.empty_cache()
                continue
            emit({"t": "error", "msg": f"warmup/inference failed: {e}"})
            return 3
    if not warmed:
        emit({"t": "error", "msg": "warmup/inference failed: out of memory even at "
              "256px tiles — close other GPU apps (or free VRAM) and retry"})
        return 3

    dec = subprocess.Popen(dec_cmd, stdout=subprocess.PIPE)
    enc = subprocess.Popen(enc_cmd, stdin=subprocess.PIPE)
    fsz = in_w * in_h * 3
    n = 0
    t0 = time.time()
    try:
        while True:
            buf = []
            for _ in range(batch):
                raw = dec.stdout.read(fsz)
                if len(raw) < fsz:
                    break
                buf.append(np.frombuffer(raw, np.uint8).reshape(in_h, in_w, 3))
            if not buf:
                break
            out_np = upscale(np.stack(buf))
            enc.stdin.write(out_np.tobytes())
            n += len(buf)
            emit({"t": "progress", "frame": n, "expected": expected})
    except BrokenPipeError:
        emit({"t": "error", "msg": "encoder pipe closed early"})
        try:
            dec.kill()
        except OSError:
            pass
        return 4
    finally:
        try:
            enc.stdin.close()
        except OSError:
            pass
        enc.wait()
        dec.wait()
    torch.cuda.synchronize()
    dt = time.time() - t0

    if enc.returncode not in (0, None):
        emit({"t": "error", "msg": f"encoder exited {enc.returncode}"})
        return 4
    emit({"t": "done", "frames": n, "secs": round(dt, 2),
          "fps": round(n / dt, 2) if dt > 0 else 0, "out": out})
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        emit({"t": "error", "msg": "usage: torch_upscale_worker.py job.json"})
        sys.exit(2)
    try:
        sys.exit(main(sys.argv[1]))
    except Exception as e:  # noqa: BLE001
        emit({"t": "error", "msg": f"worker crashed: {e}"})
        sys.exit(1)
