"""A shallow call-graph pass to recover Compose (and lambda-buried) navigation.

Compose screens don't hard-code their destinations at the click site — they take
navigation as callback lambdas, and the real `navigate(...)` / `startActivity(...)`
/ child-screen call lives in a *synthetic* class the Kotlin/Compose compiler
generates (e.g. `SongScreenKt$SongScreen$1`). Regex-on-one-file misses this because
the code is scattered across those synthetic classes.

This pass does two things a flat scan can't:
  1. Attributes every synthetic lambda/inner class back to the screen it belongs to
     (the owner is encoded in the `$Name$` chain of the class name).
  2. Treats a call to another `@Composable *Screen()` function as an edge, so
     screen-to-screen composition / navigation is recovered even with no string routes.

It also resolves `navigate("route")`, `X.class` and fragment targets found inside
those owner-attributed files. Edges are de-duplicated against what ui_flow found.
"""

from __future__ import annotations

import re

from .model import AppMap, Edge
from .scan import SourceIndex

# A composable screen *definition* vs a *call* to one.
_SCREEN_DEF = re.compile(r"\bvoid\s+([A-Z][A-Za-z0-9_]*(?:Screen|Page|Dialog|Content))\s*\(")
_SCREEN_CALL = re.compile(r"(?<!void )\b([A-Z][A-Za-z0-9_]*(?:Screen|Page|Dialog))\s*\(")
# Owner screen encoded in a synthetic class name: Foo$BarScreen$1$2 -> BarScreen
_DOLLAR_FN = re.compile(r"\$([A-Z][A-Za-z0-9_]*(?:Screen|Page|Dialog))\b")

_CLASS_LITERAL = re.compile(r"\b([A-Z][A-Za-z0-9_]*Activity\d*)\.class\b")
_FRAG_CLASS = re.compile(r"\.(?:replace|add|show)\s*\([^;{)]{0,120}?\b([A-Z][A-Za-z0-9_]*Fragment\d*)\b")
_NAVIGATE_STR = re.compile(r"""navigate\(\s*["']([A-Za-z0-9_\-/]+)["']""")
# compiled form is `NavGraphBuilderKt.composable(builder, "route", ...)` — the route
# is the 2nd arg — as well as the source form `composable("route")`.
_COMPOSABLE_STR = re.compile(
    r"""composable\(\s*(?:[\w.]+\s*,\s*)?(?:route\s*=\s*)?["']([A-Za-z0-9_\-/{}?=&.]+)["']""")


def build(m: AppMap, idx: SourceIndex, log=print) -> None:
    # function-name -> screen id, for compose screens
    fn_to_id: dict[str, str] = {}
    for s in m.screens:
        if s.kind == "compose-screen":
            fn_to_id.setdefault(s.id.rsplit("#", 1)[-1], s.id)
    if not fn_to_id:
        return  # not a Compose app — ui_flow already covers XML/intent/nav-graph flow

    simple_to_id: dict[str, str] = {}
    for s in m.screens:
        if s.kind in ("activity", "fragment"):
            simple_to_id.setdefault(s.label, s.id)

    route_to_id = _route_map(idx, fn_to_id)
    existing = {(e.src, e.dst) for e in m.edges}
    added: list[Edge] = []

    def emit(src: str, dst: str, via: str) -> None:
        if src and dst and src != dst and (src, dst) not in existing:
            existing.add((src, dst))
            added.append(Edge(src=src, dst=dst, via=via))

    def resolve_in(owner_id: str, body: str) -> None:
        for cm in _SCREEN_CALL.finditer(body):
            dst = fn_to_id.get(cm.group(1))
            if dst:
                emit(owner_id, dst, "compose")
        for cm in _CLASS_LITERAL.finditer(body):
            dst = simple_to_id.get(cm.group(1))
            if dst:
                emit(owner_id, dst, "intent")
        for cm in _FRAG_CLASS.finditer(body):
            dst = simple_to_id.get(cm.group(1))
            if dst:
                emit(owner_id, dst, "fragment")
        for cm in _NAVIGATE_STR.finditer(body):
            dst = route_to_id.get(cm.group(1))
            if dst:
                emit(owner_id, dst, "compose")

    for f in idx.files:
        if not f.first_party:
            continue
        simple = f.dotted.rsplit(".", 1)[-1]
        text = f.text
        if "$" in simple:
            # synthetic lambda/inner class -> attribute to its owner screen
            dm = _DOLLAR_FN.search(simple)
            if not dm:
                continue
            owner = fn_to_id.get(dm.group(1))
            if owner:
                resolve_in(owner, text)
        else:
            # screen definition file: attribute calls per screen-function span
            defs = [(dm.group(1), dm.start()) for dm in _SCREEN_DEF.finditer(text)]
            if not defs:
                continue
            bounds = [d[1] for d in defs] + [len(text)]
            for i, (fn, start) in enumerate(defs):
                owner = fn_to_id.get(fn)
                if owner:
                    resolve_in(owner, text[start:bounds[i + 1]])

    m.edges.extend(added)
    log(f"[*] Call graph recovered {len(added)} Compose navigation edge(s).")


def _route_map(idx: SourceIndex, fn_to_id: dict) -> dict[str, str]:
    """Best-effort route-string -> screen id, from `composable("x") { XScreen() }`
    occurrences (same file). Empty for apps that don't use Compose Navigation routes."""
    out: dict[str, str] = {}
    for f in idx.files:
        if not f.first_party or "composable(" not in f.text:
            continue
        for cm in _COMPOSABLE_STR.finditer(f.text):
            route = cm.group(1).split("/")[0].split("?")[0]
            seg = f.text[cm.end(): cm.end() + 220]
            sc = _SCREEN_CALL.search(seg)
            if sc and sc.group(1) in fn_to_id:
                out.setdefault(route, fn_to_id[sc.group(1)])
    return out
