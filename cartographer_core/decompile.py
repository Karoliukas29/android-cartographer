"""APK resolution + jadx decompilation (with a reusable cache).

Bundle handling (.xapk/.apkm/.apks) and jadx invocation mirror the lab's other
tooling so both tools behave identically on the same inputs.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import zipfile
from typing import Optional

_LAB = os.path.expanduser("~/Desktop/android-security-lab")
_JADX_CANDIDATES = [
    shutil.which("jadx"),
    os.path.join(_LAB, "tools/jadx/bin/jadx"),
]


def find_jadx() -> Optional[str]:
    for c in _JADX_CANDIDATES:
        if c and os.path.exists(c):
            return c
    return None


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve_apk(path: str, workdir: str, log=print) -> str:
    """Return a plain .apk to analyze, extracting the base APK from a bundle."""
    if path.lower().endswith(".apk") or not zipfile.is_zipfile(path):
        return path
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            apks = [n for n in names if n.lower().endswith(".apk")]
            if not apks:
                return path
            base = _pick_base_apk(z, names, apks)
            os.makedirs(workdir, exist_ok=True)
            out = os.path.join(workdir, os.path.basename(base))
            with z.open(base) as src, open(out, "wb") as dst:
                shutil.copyfileobj(src, dst)
            splits = len(apks) - 1
            log(f"[*] Bundle detected: extracted base APK '{os.path.basename(base)}'"
                + (f" ({splits} split APK(s) not analyzed)." if splits else "."))
            return out
    except Exception as e:  # noqa: BLE001
        log(f"[!] Could not unpack bundle ({e.__class__.__name__}: {e}); analyzing as-is.")
        return path


def _pick_base_apk(z: zipfile.ZipFile, names: list[str], apks: list[str]) -> str:
    if "manifest.json" in names:
        try:
            meta = json.loads(z.read("manifest.json"))
            for sp in meta.get("split_apks", []):
                if sp.get("id") == "base" and sp.get("file") in apks:
                    return sp["file"]
            pkg = meta.get("package_name")
            if pkg and f"{pkg}.apk" in apks:
                return f"{pkg}.apk"
        except Exception:  # noqa: BLE001
            pass
    if "base.apk" in apks:
        return "base.apk"
    non_config = [a for a in apks if not os.path.basename(a).lower().startswith("config.")]
    pool = non_config or apks
    return max(pool, key=lambda n: z.getinfo(n).file_size)


def decompile(apk_path: str, outdir: str, timeout: int = 420, force: bool = False,
              log=print) -> Optional[str]:
    """Run jadx into `outdir`. Returns outdir (even on partial output), None if jadx absent."""
    src = os.path.join(outdir, "sources")
    if not force and os.path.isdir(src) and any(os.scandir(src)):
        log(f"[*] Reusing cached decompilation at {outdir} (--force-decompile to refresh).")
        return outdir
    jadx = find_jadx()
    if not jadx:
        log("[!] jadx not found — mapping from manifest + resources only.")
        return None
    os.makedirs(outdir, exist_ok=True)
    cmd = [jadx, "--no-debug-info", "--no-imports", "-d", outdir, apk_path]
    log(f"[*] Decompiling with jadx (timeout {timeout}s) ...")
    try:
        subprocess.run(cmd, timeout=timeout, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)
    except subprocess.TimeoutExpired:
        log("[!] jadx timed out — mapping from whatever it produced.")
    except Exception as e:  # noqa: BLE001
        log(f"[!] jadx failed: {e}")
        return None
    return outdir
