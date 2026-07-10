"""One pass over the decompiled tree, shared by every analyzer.

Walking a large jadx `sources/` tree three times is wasteful, so we read the
relevant files once into a SourceIndex. Files under well-known third-party SDK
packages are skipped up front; everything else is kept and each file is tagged
first-party (the app's own vendor namespace) or library.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Iterable, Optional

# Big-name SDK package prefixes (path form). Obfuscated apps rename their own
# libs into short packages we can't denylist — those are handled by anchoring
# the screen graph to real manifest component names instead.
THIRD_PARTY = (
    "com/google/", "com/android/", "android/", "androidx/", "kotlin/", "kotlinx/",
    "com/facebook/", "com/squareup/", "okhttp3/", "okio/", "retrofit2/", "dagger/",
    "javax/", "org/", "io/reactivex", "io/grpc", "io/flutter", "com/bumptech/",
    "com/airbnb/", "com/adjust/", "com/amplitude/", "com/appsflyer/", "j$/",
    "com/onesignal/", "com/mixpanel/", "com/braze/", "_COROUTINE/", "hilt_aggregated",
    "dagger/", "com/stripe/android/", "com/google/firebase/", "com/jakewharton/",
)


@dataclass
class SourceFile:
    path: str
    rel: str            # package-relative path (com/app/ui/LoginActivity.java)
    dotted: str         # com.app.ui.LoginActivity
    text: str
    first_party: bool


@dataclass
class SourceIndex:
    outdir: Optional[str]
    files: list[SourceFile] = field(default_factory=list)
    total_files: int = 0
    library_files: int = 0

    @property
    def first_party(self) -> list[SourceFile]:
        return [f for f in self.files if f.first_party]

    def by_dotted(self) -> dict:
        return {f.dotted: f for f in self.files}


def _after_root(path: str) -> str:
    for marker in ("/sources/", "/resources/"):
        i = path.find(marker)
        if i != -1:
            return path[i + len(marker):]
    return os.path.basename(path)


def _iter(base: str, exts: tuple[str, ...]) -> Iterable[str]:
    for dirpath, _dirs, files in os.walk(base):
        for fn in files:
            if fn.endswith(exts):
                yield os.path.join(dirpath, fn)


def _vendor_roots(package: str) -> tuple[str, ...]:
    """Path-form roots considered 'the app's own code'."""
    roots = set()
    if package:
        roots.add(package.replace(".", "/") + "/")
        if package.count(".") >= 1:
            roots.add("/".join(package.split(".")[:2]) + "/")
    return tuple(roots)


def build_index(outdir: Optional[str], package: str, log=print) -> SourceIndex:
    idx = SourceIndex(outdir=outdir)
    if not outdir:
        return idx
    src = os.path.join(outdir, "sources")
    base = src if os.path.isdir(src) else outdir
    if not os.path.isdir(base):
        return idx
    vendor = _vendor_roots(package)

    total = kept = lib = 0
    for path in _iter(base, (".java", ".kt")):
        total += 1
        rel = _after_root(path)
        if rel.startswith(THIRD_PARTY):
            lib += 1
            continue
        first_party = bool(vendor) and rel.startswith(vendor)
        if not first_party:
            lib += 1
        try:
            if os.path.getsize(path) > 3_000_000:
                continue
            with open(path, "r", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        dotted = rel[:-5].replace("/", ".") if rel.endswith((".java", ".kt")) else rel.replace("/", ".")
        idx.files.append(SourceFile(path=path, rel=rel, dotted=dotted, text=text,
                                    first_party=first_party))
        kept += 1
    idx.total_files = total
    idx.library_files = lib
    log(f"[*] Indexed {kept} code file(s) of {total} "
        f"({sum(1 for f in idx.files if f.first_party)} first-party, {total - kept} SDK skipped).")
    return idx


# ---------------------------------------------------------------------------
# Framework + obfuscation detection
# ---------------------------------------------------------------------------
_FRAMEWORKS = [
    ("Flutter", "compiled Dart in libapp.so / libflutter.so (native)",
     ("assets/flutter_assets/",)),
    ("React Native", "a JavaScript / Hermes bundle (assets/index.android.bundle)",
     ("assets/index.android.bundle", "assets/index.bundle")),
    ("Unity", "compiled C# via IL2CPP in libil2cpp.so (native)", ("assets/bin/Data/",)),
    ("Xamarin / .NET MAUI", "compiled .NET assemblies", ("assemblies/", "assemblies.blob")),
    ("Cordova / Ionic", "web assets under assets/www (HTML/JS)", ("assets/www/",)),
]


def detect_framework(apk) -> tuple[str, str]:
    try:
        files = set(apk.get_files())
    except Exception:  # noqa: BLE001
        files = set()
    for name, logic, assets in _FRAMEWORKS:
        if any(any(f.startswith(a) for f in files) for a in assets):
            return name, (f"This is a {name} app — its core logic lives in {logic}, which a "
                          f"Java/Kotlin decompiler cannot read. The Java-side map below is the "
                          f"host shell only; drive the real logic with framework-specific tooling.")
    return "Native (Java/Kotlin)", ""


def detect_obfuscation(idx: SourceIndex) -> tuple[bool, str]:
    """Heuristic: a high share of 1–2 char class names => name obfuscation (R8/ProGuard)."""
    names = []
    for f in idx.files:
        cls = f.dotted.rsplit(".", 1)[-1]
        names.append(cls)
    if len(names) < 40:
        return False, ""
    short = sum(1 for n in names if len(n) <= 2)
    ratio = short / max(1, len(names))
    if ratio >= 0.35:
        return True, (f"Name obfuscation detected (~{ratio:.0%} of classes have 1–2 char names, "
                      f"R8/ProGuard). Class names in the map are meaningless, but the flow is still "
                      f"reconstructed from manifest component names, resource IDs, and network "
                      f"strings — all of which survive obfuscation.")
    return False, ""
