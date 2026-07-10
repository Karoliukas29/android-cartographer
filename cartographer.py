#!/usr/bin/env python3
"""Android Cartographer — map how an Android app is built and how a user moves
through it.

    python3 cartographer.py <app.apk | app.xapk> [-o out/] [--force-decompile]

Static + offline. Point it at an APK (obfuscated or not) and it reconstructs the
screen flow, the widgets on each screen, the network surface, the class/tech-stack
map, storage, permissions and third-party SDKs — into a self-contained HTML report
plus report.json.

No API keys, no network calls, no secrets. Analyze only apps you are allowed to.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from cartographer_core import (decompile, manifest, scan, ui_flow, callgraph,
                               network, classes, insight, report)


def _default_outdir(pkg: str, apk_path: str) -> str:
    name = pkg or os.path.splitext(os.path.basename(apk_path))[0]
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "carto-out", name)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="cartographer.py",
        description="Map an Android app's screens, flow, network and architecture.")
    p.add_argument("apk", help="path to .apk / .xapk / .apkm / .apks")
    p.add_argument("-o", "--outdir", default=None,
                   help="output directory (default: carto-out/<package>/)")
    p.add_argument("--decompile-timeout", type=int, default=420,
                   help="jadx timeout in seconds (default 420)")
    p.add_argument("--force-decompile", action="store_true",
                   help="ignore any cached decompilation and re-run jadx")
    p.add_argument("--json-only", action="store_true",
                   help="write report.json only (skip HTML + terminal summary)")
    p.add_argument("-q", "--quiet", action="store_true", help="suppress progress logs")
    args = p.parse_args(argv)

    def log(*a):
        if not args.quiet:
            print(*a)

    if not os.path.exists(args.apk):
        print(f"error: file not found: {args.apk}", file=sys.stderr)
        return 2

    t0 = time.time()
    outdir = args.outdir or _default_outdir("", args.apk)
    os.makedirs(outdir, exist_ok=True)

    # 1. Resolve bundle -> base apk, then parse the manifest (needs package for outdir).
    apk_path = decompile.resolve_apk(args.apk, os.path.join(outdir, "_bundle"), log=log)
    try:
        m, apk, root = manifest.load(apk_path, log=log)
    except Exception as e:  # noqa: BLE001
        print(f"error: could not parse APK ({e.__class__.__name__}: {e})", file=sys.stderr)
        return 1

    # Re-home output under the real package name if the user didn't pin one.
    if not args.outdir and m.package:
        newout = _default_outdir(m.package, apk_path)
        if os.path.abspath(newout) != os.path.abspath(outdir):
            os.makedirs(newout, exist_ok=True)
            outdir = newout
    log(f"[*] Package: {m.package or '(unknown)'}  ->  {outdir}")

    # 2. Framework check (before decompiling — flags native/cross-platform apps).
    m.framework, m.framework_note = scan.detect_framework(apk)

    # 3. Decompile + index sources.
    dec = decompile.decompile(apk_path, os.path.join(outdir, "decompiled"),
                              timeout=args.decompile_timeout, force=args.force_decompile, log=log)
    idx = scan.build_index(dec, m.package, log=log)
    m.obfuscated, m.obfuscation_note = scan.detect_obfuscation(idx)

    # 4. Analyzers.
    ui_flow.build(m, idx, dec, log=log)
    callgraph.build(m, idx, log=log)
    network.build(m, idx, log=log)
    classes.build(m, idx, log=log)
    insight.build(m, idx, log=log)

    # 5. Output.
    json_path = os.path.join(outdir, "report.json")
    report.to_json_file(m, json_path)
    if not args.json_only:
        html_path = os.path.join(outdir, "report.html")
        report.to_html_file(m, html_path)
        print()
        report.to_terminal(m, log=print)
        print(f"\n[+] HTML report : {html_path}")
        print(f"[+] JSON report : {json_path}")
    else:
        print(json_path)

    log(f"[*] Done in {time.time()-t0:.1f}s.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
