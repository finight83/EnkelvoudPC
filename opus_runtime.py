#!/usr/bin/env python3
"""
opus_runtime.py - make a working Opus encoder available, installing it if needed.

Why this exists
---------------
`pip install opuslib` installs only the ctypes *binding*. The binding then asks
`ctypes.util.find_library("opus")` where the native library is, and raises

    Exception: Could not find Opus library. Make sure it is installed.

on a machine that has never had libopus. That is every fresh Windows box:
there is no Opus DLL on it and no package manager that would put one there.
So the PC server could not encode a single packet out of the box.

What it does
------------
1. Prefer `opuslib_next` + `opuslib-next-bundled`: a wheel that carries the
   native library itself (`opuslib_next/_native/opus.dll`, and the .so/.dylib
   on Linux and macOS), so there is nothing to find on the system and nothing
   to trust from a random download URL.
2. Fall back to `opuslib` when the system already has libopus (Debian/Ubuntu
   `libopus0`, Fedora `opus`, `brew install opus`) -- then nothing is
   downloaded at all.
3. If neither is importable and auto-install is allowed, `pip install
   opuslib-next-bundled` into the running interpreter and import it.

The rest of the code never has to care which one it got: `ensure_opus()`
returns a module with `Encoder`/`Decoder`, which is the whole surface
`enkelvoud/encoder.py` uses.

Usage
-----
    import opus_runtime
    opus = opus_runtime.ensure_opus()     # module, or None; never raises
    enc = opus.Encoder(48000, 2, "audio")

or from a shell:  python opus_runtime.py --check | --install
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from typing import Any, Optional

# Package that ships the native library inside the wheel. Pinned to a minor
# range: the C API it exposes is what encoder.py was written against.
BUNDLED_PACKAGE = "opuslib-next-bundled>=0.1.1,<0.2"

_MODULE: Optional[Any] = None
_RESOLVED = False


def _try_import(name: str):
    try:
        return importlib.import_module(name)
    except BaseException:      # opuslib raises a plain Exception, not ImportError
        return None


def _usable(mod) -> bool:
    """A module we can actually encode with."""
    if mod is None or not hasattr(mod, "Encoder"):
        return False
    try:
        enc = mod.Encoder(48000, 2, "audio")
    except BaseException:
        return False
    try:
        if hasattr(enc, "destroy"):
            enc.destroy()
    except BaseException:
        pass
    return True


def _pip_install(spec: str) -> bool:
    print(f"[opus] installing {spec} (it carries the native library)",
          file=sys.stderr)
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
             spec],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
    except OSError as e:
        print(f"[opus] pip could not be started: {e}", file=sys.stderr)
        return False
    if r.returncode != 0:
        tail = "\n".join((r.stdout or "").strip().splitlines()[-6:])
        print(f"[opus] pip install failed:\n{tail}", file=sys.stderr)
        return False
    return True


def ensure_opus(auto_install: bool = True) -> Optional[Any]:
    """Return a module with Encoder/Decoder, installing one if needed.

    Never raises. Returns None only when nothing could be made to work, and
    then it has already printed the one command that fixes it.
    """
    global _MODULE, _RESOLVED
    if _RESOLVED:
        return _MODULE
    _RESOLVED = True

    # 1. Already installed and working (bundled wheel, or a system libopus).
    for name in ("opuslib_next", "opuslib"):
        mod = _try_import(name)
        if _usable(mod):
            _MODULE = mod
            return mod

    # 2. Nothing usable. Install the self-contained wheel, unless told not to.
    if not auto_install:
        print("[opus] no working Opus encoder and auto-install is off. "
              f"Fix:  {sys.executable} -m pip install {BUNDLED_PACKAGE}",
              file=sys.stderr)
        return None

    if not _pip_install(BUNDLED_PACKAGE):
        print("[opus] Opus is unavailable, so audio encoding cannot run. "
              f"Install it by hand:  {sys.executable} -m pip install "
              f"{BUNDLED_PACKAGE}", file=sys.stderr)
        return None

    importlib.invalidate_caches()
    mod = _try_import("opuslib_next") or _try_import("opuslib")
    if _usable(mod):
        print("[opus] native Opus library ready", file=sys.stderr)
        _MODULE = mod
        return mod

    print("[opus] installed the package but it still will not encode. On "
          "Linux/macOS the system library may be missing: apt install "
          "libopus0 / brew install opus", file=sys.stderr)
    return None


def main(argv) -> int:
    if "--check" in argv:
        mod = ensure_opus(auto_install=False)
        if mod:
            print(f"opus: OK ({mod.__name__})")
            return 0
        print("opus: NOT AVAILABLE")
        return 1
    mod = ensure_opus(auto_install="--no-install" not in argv)
    print(f"opus: {'OK (' + mod.__name__ + ')' if mod else 'FAILED'}")
    return 0 if mod else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
