"""Isolated Whisper engine — a dedicated venv the Suite owns and nothing else shares.

WHY THIS EXISTS (2026-08-21)
────────────────────────────
Until now the Suite installed whisper backends with
`pip install --user --break-system-packages`, straight into the user's shared
site-packages. On 2026-08-18 that did real damage on the developer's own machine:
installing WhisperX for the Suite pulled `transformers` backwards, which made pip
select a year-old whisperx, which hard-pinned `faster-whisper==1.1.0` and a cuDNN 8
`ctranslate2` — and **took the speech-to-text of an unrelated always-on voice
assistant offline**, because it ran `/usr/bin/python3` with no venv and shared those
same packages. It stayed broken-but-working until the next restart, so the cause and
the symptom were days apart.

Tony's point, which is the real reason this module exists: *"someone else, especially
if they have a GPU, might have other applications that rely on whisper."* A GPU owner
is exactly the person most likely to already have torch installed for their own work.
Pinning more carefully only makes that rarer. **Only isolation makes it impossible.**

Pip running inside a venv cannot modify anything outside it. That is a structural
guarantee, not a mitigation.

DESIGN NOTES
────────────
* Mirrors `torch_upscaler.py`, which already solved this for torch — same venv-on-demand
  shape, same "probe before declaring success" rule. Deliberately NOT a new pattern.
* A SEPARATE venv from the upscaler's, on purpose. Sharing one saves a few GB but
  re-couples them: if whisperx ever wants a torch the upscaler dislikes, both break and
  we are resolving dependency conflicts again — the exact problem being escaped, just
  moved. Disk is the cheap resource here.
* ⚠️ `ctranslate2>=4.5` is explicit and load-bearing. Without it pip can backtrack to a
  cuDNN 8 build that will not load on a cuDNN 9 system.
* The caller shows the user a full disclosure BEFORE anything downloads — see
  `install_plan()`. They get the size, the location, what is NOT touched, and how to
  undo it, and then they decide.
"""

import os
import shutil
import subprocess
import sys

VENV_DIR = os.path.join(
    os.path.expanduser("~/.local/share/docflix"), "whisper-engine", "venv")

# PyTorch's CPU-only wheel index. A machine with no NVIDIA GPU has no use for ~5 GB of
# CUDA libraries, and torch pulls them by default.
CPU_INDEX = "https://download.pytorch.org/whl/cpu"

# Rough download sizes, so the disclosure can quote a real number instead of "several
# minutes". CUDA is dominated by the nvidia-* wheels torch depends on.
SIZE_GPU_GB = 7
SIZE_CPU_MB = 900


def venv_python():
    """Path to the engine's interpreter, or None if the engine is not built."""
    p = os.path.join(VENV_DIR, "bin", "python")
    return p if os.path.exists(p) else None


def is_installed():
    return venv_python() is not None


def has_nvidia_gpu():
    """True if an NVIDIA GPU is actually usable — not merely that a driver is present.

    Deliberately shells out to nvidia-smi rather than importing torch: at the moment
    this is called there may be no torch anywhere to import.
    """
    if not shutil.which("nvidia-smi"):
        return False
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=15)
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        return False


def install_plan():
    """Everything the user needs to decide, gathered BEFORE anything is downloaded.

    Returns a dict the UI turns into a disclosure dialog. Tony, 2026-08-21:
    *"the user should know and have the info in-hand before making that decision so
    they know what they are getting into."*
    """
    gpu = has_nvidia_gpu()
    return {
        "gpu": gpu,
        "size_human": f"~{SIZE_GPU_GB} GB" if gpu else f"~{SIZE_CPU_MB} MB",
        "location": VENV_DIR,
        "packages": ["whisperx", "faster-whisper", "ctranslate2>=4.5", "torch", "torchaudio"],
        "accel": ("NVIDIA GPU detected — installing the CUDA build for hardware acceleration"
                  if gpu else
                  "No NVIDIA GPU detected — installing the CPU-only build "
                  "(far smaller; transcription will be slower)"),
        "free_human": _human_bytes(shutil.disk_usage(os.path.expanduser("~")).free),
    }


def _human_bytes(n):
    """'3.9 TB', not '3938.2 GB' — a disclosure people have to read at a glance."""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def disclosure_text(plan):
    """The dialog body. Plain language, real numbers, and how to undo it."""
    return (
        f"The Whisper speech-recognition engine is not installed yet.\n\n"
        f"WHAT WILL BE DOWNLOADED\n"
        f"  {plan['size_human']} — whisperx, faster-whisper, ctranslate2, torch\n"
        f"  {plan['accel']}\n\n"
        f"WHERE IT GOES\n"
        f"  {plan['location']}\n"
        f"  This is a self-contained environment used only by this application.\n\n"
        f"WHAT IS NOT TOUCHED\n"
        f"  Your system Python and any packages you already have installed.\n"
        f"  If you use torch, whisper or transformers for anything else, this\n"
        f"  cannot change or break them.\n\n"
        f"TO REMOVE IT LATER\n"
        f"  Delete that folder. Nothing else on your system is modified.\n\n"
        f"Disk free: {plan['free_human']}\n\n"
        f"Install now?"
    )


def build(log=None, progress=None):
    """Create the venv and install the engine. Raises RuntimeError on failure.

    log(msg, level) and progress(pct, msg) are optional UI callbacks.
    """
    def _log(m, lvl="INFO"):
        if log:
            log(m, lvl)

    def _prog(p, m):
        if progress:
            progress(p, m)

    gpu = has_nvidia_gpu()
    os.makedirs(os.path.dirname(VENV_DIR), exist_ok=True)

    _prog(2, "Creating an isolated Python environment...")
    _log(f"Creating venv at {VENV_DIR}", "INFO")
    try:
        subprocess.run([sys.executable, "-m", "venv", VENV_DIR],
                       check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Could not create the environment: {e.stderr or e}")

    py = os.path.join(VENV_DIR, "bin", "python")

    # ⚠️ ctranslate2>=4.5 is REQUIRED, not cosmetic. Older builds link cuDNN 8; a
    # cuDNN 9 system cannot load them, and the failure appears only at first
    # inference — long after the install reports success.
    pkgs = ["whisperx", "ctranslate2>=4.5"]
    cmd = [py, "-m", "pip", "install", "--upgrade"] + pkgs
    if not gpu:
        cmd += ["--extra-index-url", CPU_INDEX]
        _log("No NVIDIA GPU — using the CPU-only wheel index", "INFO")

    _prog(8, f"Downloading and installing ({'~7 GB, ' if gpu else ''}several minutes)...")
    _log("pip install whisperx 'ctranslate2>=4.5' — streaming below:", "INFO")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            _log("  " + line)
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError("Install failed — see the log above.")

    _prog(92, "Verifying the engine...")
    ok, detail = probe()
    if not ok:
        raise RuntimeError(f"Installed, but the engine does not work: {detail}")

    _log("Whisper engine ready.", "SUCCESS")
    _prog(100, "Done.")
    return VENV_DIR


def probe():
    """Verify the engine actually WORKS. Never report success from an exit code alone.

    ⚠️ This exists because on 2026-08-18 a whisper install completed cleanly and then
    failed at first inference — ctranslate2 could not find cuDNN. An install that
    "succeeded" and cannot transcribe is worse than one that failed loudly.
    """
    py = venv_python()
    if not py:
        return False, "not installed"
    code = (
        "import ctranslate2, faster_whisper, whisperx, torch;"
        "print('CT2', ctranslate2.__version__);"
        "print('CUDA', ctranslate2.get_cuda_device_count());"
        "print('TORCH', torch.__version__)"
    )
    try:
        r = subprocess.run([py, "-c", code], capture_output=True, text=True, timeout=180)
    except Exception as e:
        return False, str(e)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "import failed").strip()[:300]
    return True, r.stdout.strip().replace("\n", ", ")


def uninstall():
    """Delete the engine. Nothing outside VENV_DIR is touched — that is the point."""
    if os.path.isdir(VENV_DIR):
        shutil.rmtree(VENV_DIR, ignore_errors=True)
        return True
    return False


def ensure_engine_ui(parent, log=None):
    """Check the engine; if missing, show the FULL disclosure and offer to build it.

    Returns True when the engine is ready to use.

    ⚠️ This is the single place the user is asked. Every whisper entry point in the
    Suite routes through here so they all say the same thing — previously three
    different dialogs quoted three different sizes ("~200MB", "~2GB", "several
    minutes") and all three silently modified the user's system Python.

    Tony, 2026-08-21: *"the user should know and have the info in-hand before making
    that decision so they know what they are getting into."*
    """
    if is_installed():
        return True

    # Imported lazily: this module is also read by non-GUI code paths.
    from tkinter import messagebox

    plan = install_plan()
    if not messagebox.askyesno("Install the Whisper engine?",
                               disclosure_text(plan), parent=parent):
        return False

    def _log(m, lvl="INFO"):
        if log:
            log(m, lvl)
        else:
            print(m)

    try:
        build(log=_log)
    except Exception as e:
        messagebox.showerror("Install failed", str(e), parent=parent)
        return False

    ok, detail = probe()
    if not ok:
        messagebox.showerror(
            "Engine not working",
            f"The install completed but the engine failed its check:\n\n{detail}",
            parent=parent)
        return False
    return True
