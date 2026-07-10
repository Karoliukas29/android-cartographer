"""Reconstruct the user journey: screens, their widgets, and the transitions.

Signals used (all survive obfuscation to varying degrees):
  * manifest activities                     -> canonical screen nodes (real names)
  * res/layout/*.xml                        -> buttons / inputs per screen
  * res/navigation/*.xml (Nav component)    -> fragment destinations + actions
  * setContentView(R.layout.x) / *Binding   -> screen <-> layout wiring
  * new Intent(ctx, X.class) / X::class.java -> activity->activity edges
  * navController.navigate("route")          -> Compose / route edges
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from typing import Optional

from .model import AppMap, Screen, Edge, Widget
from .scan import SourceIndex, SourceFile

ANDROID_NS = "http://schemas.android.com/apk/res/android"
APP_NS = "http://schemas.android.com/apk/res-auto"
_A = f"{{{ANDROID_NS}}}"
_APP = f"{{{APP_NS}}}"

_BUTTON_TAGS = ("Button", "ImageButton", "FloatingActionButton", "MaterialButton",
                "ExtendedFloatingActionButton", "Chip")
_INPUT_TAGS = ("EditText", "TextInputEditText", "AutoCompleteTextView", "CheckBox",
               "RadioButton", "Switch", "SwitchMaterial", "ToggleButton", "Spinner",
               "SeekBar", "RatingBar", "TextInputLayout")


def build(m: AppMap, idx: SourceIndex, outdir: Optional[str], log=print) -> None:
    screens: dict[str, Screen] = {}
    activity_by_simple: dict[str, str] = {}
    vendor = _vendor_prefix(m.package)

    # 1. Seed screens from manifest activities (canonical, real names). SDK-injected
    #    activities (Firebase/GMS auth flows, etc.) are dropped from the graph so it
    #    reflects the app's own journey — they remain in the components inventory.
    for c in m.components:
        if c.kind != "Activity":
            continue
        if not _own_class(c.name, m.package, vendor):
            continue
        sid = c.name
        simple = sid.rsplit(".", 1)[-1]
        screens[sid] = Screen(id=sid, label=simple, kind="activity",
                              is_entry=c.is_launcher, exported=c.exported)
        activity_by_simple.setdefault(simple, sid)
    m.entry_screens = [s.id for s in screens.values() if s.is_entry]

    # 2. Parse layouts -> widgets, keyed by layout resource name.
    layouts = _parse_layouts(outdir)

    # 3. Wire activities to layouts + attach fragments discovered in source.
    edges: list[Edge] = []
    for f in idx.files:
        _wire_screen(f, screens, layouts, activity_by_simple)

    # 3b. Jetpack Compose: real screens are @Composable *Screen() functions with no
    #     XML layout — detect them and pull their Compose widgets.
    for f in idx.files:
        _compose_screens(f, screens)

    # Index screen names now that activities, fragments and compose screens exist —
    # used to resolve navigation targets (fragment transactions, navigate(id), ...).
    by_simple: dict[str, str] = {}
    by_lower: dict[str, str] = {}
    for sid, scr in screens.items():
        by_simple.setdefault(scr.label, sid)
        by_lower.setdefault(scr.label.lower(), sid)

    # 4. Navigation edges: intents, fragment transactions, navigate(id), compose routes.
    for f in idx.files:
        _intent_edges(f, screens, activity_by_simple, edges)
        _fragment_edges(f, screens, by_simple, by_lower, edges)
        _compose_edges(f, screens, edges)

    # 5. Nav-component graph (res/navigation/*.xml) — fragments + actions.
    _nav_graph_edges(outdir, screens, edges)

    # 5b. Bind each button to the screen it navigates to (best-effort).
    _bind_widgets(screens, idx, by_simple, by_lower, edges)

    # Deduplicate edges.
    seen = set()
    uniq: list[Edge] = []
    for e in edges:
        key = (e.src, e.dst, e.via)
        if key in seen or e.src == e.dst:
            continue
        seen.add(key)
        uniq.append(e)

    m.screens = sorted(screens.values(), key=lambda s: (not s.is_entry, s.kind, s.label.lower()))
    m.edges = uniq
    log(f"[*] Mapped {len(m.screens)} screen(s) and {len(m.edges)} transition(s).")


# ---------------------------------------------------------------------------
# Layouts
# ---------------------------------------------------------------------------
def _parse_layouts(outdir: Optional[str]) -> dict[str, list[Widget]]:
    out: dict[str, list[Widget]] = {}
    if not outdir:
        return out
    for cand in ("resources/res", "res"):
        base = os.path.join(outdir, cand)
        if os.path.isdir(base):
            break
    else:
        return out
    for dirpath, _dirs, files in os.walk(base):
        if os.path.basename(dirpath).split("-")[0] != "layout":
            continue
        for fn in files:
            if not fn.endswith(".xml"):
                continue
            name = fn[:-4]
            widgets = _widgets_from_layout(os.path.join(dirpath, fn))
            # keep the richest variant if a layout has qualifiers
            if name not in out or len(widgets) > len(out[name]):
                out[name] = widgets
    return out


def _widgets_from_layout(path: str) -> list[Widget]:
    try:
        tree = ET.parse(path)
    except Exception:  # noqa: BLE001
        return []
    out: list[Widget] = []
    for el in tree.iter():
        tag = el.tag.rsplit("}", 1)[-1].rsplit(".", 1)[-1]
        wid = _res_id(el.get(f"{_A}id", ""))
        onclick = el.get(f"{_A}onClick", "")
        text = el.get(f"{_A}text", "") or el.get(f"{_A}hint", "")
        text = _clean_ref(text)
        is_button = tag in _BUTTON_TAGS or tag.endswith("Button") or bool(onclick)
        is_input = tag in _INPUT_TAGS
        if not (is_button or is_input) and not wid:
            continue
        if is_button:
            out.append(Widget(wid=wid or "(unnamed)", kind=tag, text=text, onclick=onclick))
        elif is_input:
            out.append(Widget(wid=wid or "(unnamed)", kind=tag, text=text))
    return out


def _res_id(raw: str) -> str:
    return re.sub(r"^@\+?id/", "", raw) if raw else ""


def _clean_ref(raw: str) -> str:
    if not raw:
        return ""
    if raw.startswith("@"):
        return raw.rsplit("/", 1)[-1]
    return raw[:40]


# ---------------------------------------------------------------------------
# Screen <-> layout wiring, fragments
# ---------------------------------------------------------------------------
_SETCONTENT = re.compile(r"setContentView\(\s*R\.layout\.([A-Za-z0-9_]+)")
_INFLATE = re.compile(r"inflate\(\s*R\.layout\.([A-Za-z0-9_]+)")
_BINDING = re.compile(r"([A-Za-z0-9]+)Binding\.inflate")
_FRAGMENT_SUPER = re.compile(r"\b(?:extends|:)\s*[A-Za-z0-9_.]*Fragment\b")
_FRAG_NAME = re.compile(r"Fragment\d*$")


def _is_real_fragment(simple: str, text: str) -> bool:
    """A concrete, top-level Fragment subclass — not an inner-class lambda
    (Foo$onCreateView$1), a Compose singleton, a Hilt wrapper, a generated
    *FragmentArgs/*Binding, or an abstract base."""
    if "$" in simple or simple.startswith(("Hilt_", "ComposableSingletons")):
        return False
    if not _FRAG_NAME.search(simple):          # must END in Fragment (optionally Fragment2)
        return False
    if re.search(r"\babstract\s+class\s+" + re.escape(simple) + r"\b", text):
        return False
    return True


def _camel_to_layout(name: str) -> str:
    s = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
    return s


def _wire_screen(f: SourceFile, screens: dict, layouts: dict, activity_by_simple: dict) -> None:
    simple = f.dotted.rsplit(".", 1)[-1]
    text = f.text
    # Activity already a screen?
    sid = None
    if f.dotted in screens:
        sid = f.dotted
    elif simple in activity_by_simple:
        sid = activity_by_simple[simple]

    layout_name = None
    mm = _SETCONTENT.search(text)
    if mm:
        layout_name = mm.group(1)
    if not layout_name:
        mb = _BINDING.search(text)
        if mb:
            cand = _camel_to_layout(mb.group(1))
            if cand in layouts:
                layout_name = cand

    # Fragment discovery (real top-level fragments only)
    if sid is None and f.first_party and _is_real_fragment(simple, text):
        sid = f.dotted
        screens[sid] = Screen(id=sid, label=simple, kind="fragment")
        mi = _INFLATE.search(text)
        if mi:
            layout_name = mi.group(1)

    if sid is None:
        return
    scr = screens[sid]
    if layout_name and not scr.layout:
        scr.layout = layout_name
        widgets = layouts.get(layout_name, [])
        scr.buttons = [w for w in widgets if w.kind in _BUTTON_TAGS or w.kind.endswith("Button") or w.onclick][:40]
        scr.inputs = [w for w in widgets if w not in scr.buttons][:40]


# ---------------------------------------------------------------------------
# Navigation edges
# ---------------------------------------------------------------------------
# jadx emits `new android.content.Intent(ctx, (java.lang.Class<?>) com.x.Y.class)`
# and ternary targets, so we don't try to parse the whole call — we grab every
# `Foo.class` / `Foo::class.java` literal in files that navigate, and keep the
# ones that resolve to a known activity.
_NAV_HINT = re.compile(r"startActivit|new\s+[\w.]*Intent|::class\.java")
_CLASS_LITERAL = re.compile(r"([A-Za-z_][\w.]*)\.class\b")
_KT_CLASS = re.compile(r"([A-Za-z_][\w.]*)::class\.java\b")


def _vendor_prefix(pkg: str) -> str:
    if not pkg:
        return ""
    return ".".join(pkg.split(".")[:2]) if pkg.count(".") >= 1 else pkg


def _own_class(name: str, pkg: str, vendor: str) -> bool:
    return (name == pkg or name.startswith(pkg + ".")
            or (bool(vendor) and name.startswith(vendor + ".")))


def _screen_id_for_file(f: SourceFile, screens: dict, activity_by_simple: dict) -> Optional[str]:
    base = f.dotted.split("$", 1)[0]           # attribute inner-class lambdas to the outer class
    if f.dotted in screens:
        return f.dotted
    if base in screens:
        return base
    simple = f.dotted.rsplit(".", 1)[-1]
    outer = simple.split("$", 1)[0]
    return activity_by_simple.get(simple) or activity_by_simple.get(outer)


def _resolve_target(raw: str, screens: dict, activity_by_simple: dict) -> Optional[str]:
    raw = raw.strip()
    if raw in screens:
        return raw
    simple = raw.rsplit(".", 1)[-1]
    return activity_by_simple.get(simple)


def _intent_edges(f: SourceFile, screens: dict, activity_by_simple: dict, edges: list) -> None:
    if not _NAV_HINT.search(f.text):
        return
    src = _screen_id_for_file(f, screens, activity_by_simple)
    targets = set()
    for rx in (_CLASS_LITERAL, _KT_CLASS):
        for mm in rx.finditer(f.text):
            targets.add(mm.group(1))
    for t in targets:
        dst = _resolve_target(t, screens, activity_by_simple)
        if not dst:
            continue
        # If the launching file isn't itself a screen, add it as a lightweight node.
        if src is None:
            simple = f.dotted.rsplit(".", 1)[-1]
            if not f.first_party or "$" in simple:
                continue
            src = f.dotted
            screens.setdefault(src, Screen(id=src, label=simple, kind="class"))
        edges.append(Edge(src=src, dst=dst, via="intent"))


# ---- fragment transactions + navigate(id) -----------------------------------
# `getSupportFragmentManager().beginTransaction().replace(R.id.container, new
# HomeFragment())` and `navController.navigate(R.id.detailFragment)`. We resolve
# the target to a KNOWN first-party fragment, so library transactions (exoplayer,
# gms LifecycleFragment, ...) don't resolve and produce no noise.
_FRAG_TXN = re.compile(r"\.(?:replace|add|show|navigate)\s*\(")
_FRAG_CLASS = re.compile(r"\b([A-Z][A-Za-z0-9_]*Fragment\d*)\b")
_NAV_RESID = re.compile(r"navigate\(\s*(?:R\.id\.)?([A-Za-z][A-Za-z0-9_]*)\s*[,)]")


def _file_screen(f: SourceFile, screens: dict, by_simple: dict) -> Optional[str]:
    base = f.dotted.split("$", 1)[0]
    if f.dotted in screens:
        return f.dotted
    if base in screens:
        return base
    simple = f.dotted.rsplit(".", 1)[-1].split("$", 1)[0]
    return by_simple.get(simple)


def _resolve_nav_id(name: str, by_simple: dict, by_lower: dict) -> Optional[str]:
    # navigate(R.id.homeFragment) / navigate(R.id.home_fragment) -> HomeFragment
    if name.startswith("action") or name.startswith("nav_"):
        return None
    cand = by_lower.get(name.lower())
    if cand:
        return cand
    camel = "".join(p.capitalize() for p in name.split("_"))
    return by_simple.get(camel) or by_lower.get(camel.lower())


def _fragment_edges(f: SourceFile, screens: dict, by_simple: dict, by_lower: dict,
                    edges: list) -> None:
    text = f.text
    if "Fragment" not in text and "navigate(" not in text:
        return
    src = _file_screen(f, screens, by_simple)
    if not src:
        return
    seen = set()
    # fragment transactions: capture the fragment class named right after replace/add/show
    for m in _FRAG_TXN.finditer(text):
        fc = _FRAG_CLASS.search(text, m.end(), m.end() + 160)
        if not fc:
            continue
        dst = by_simple.get(fc.group(1))
        if dst and dst != src and (src, dst) not in seen:
            seen.add((src, dst))
            edges.append(Edge(src=src, dst=dst, via="fragment"))
    # navigate(R.id.<destination>)
    for m in _NAV_RESID.finditer(text):
        dst = _resolve_nav_id(m.group(1), by_simple, by_lower)
        if dst and dst != src and (src, dst) not in seen:
            seen.add((src, dst))
            edges.append(Edge(src=src, dst=dst, via="nav"))


# ---- button -> action binding ------------------------------------------------
_PRIMARY = ("login", "signin", "sign_in", "signup", "sign_up", "register", "submit",
            "continue", "next", "start", "go", "save", "confirm", "done", "pay",
            "checkout", "proceed", "enter", "search", "add", "create", "send")


def _camel(wid: str) -> str:
    parts = wid.split("_")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


def _nav_positions(text: str, by_simple: dict, by_lower: dict) -> list:
    """(position, target_label) for each navigation reference in the file."""
    out = []
    for mm in re.finditer(r"\b([A-Z][A-Za-z0-9_]*(?:Activity|Fragment)\d*)\b", text):
        sid = by_simple.get(mm.group(1))
        if sid:
            out.append((mm.start(), sid))
    for mm in _NAV_RESID.finditer(text):
        sid = _resolve_nav_id(mm.group(1), by_simple, by_lower)
        if sid:
            out.append((mm.start(), sid))
    return sorted(out)


def _nearest_after(navs: list, pos: int, window: int) -> Optional[str]:
    for p, sid in navs:
        if pos <= p <= pos + window:
            return sid
    return None


def _is_primary(b: Widget) -> bool:
    tok = (b.wid + " " + b.text).lower()
    return any(k in tok for k in _PRIMARY)


def _bind_widgets(screens: dict, idx: SourceIndex, by_simple: dict, by_lower: dict,
                  edges: list) -> None:
    file_by_dotted = {f.dotted: f for f in idx.files}
    label_of = {sid: s.label for sid, s in screens.items()}
    out_targets: dict[str, list] = defaultdict(list)
    for e in edges:
        if e.src in screens and e.dst in screens:
            out_targets[e.src].append(e.dst)

    for sid, scr in screens.items():
        if scr.kind not in ("activity", "fragment") or not scr.buttons:
            continue
        f = file_by_dotted.get(sid)
        text = f.text if f else ""
        # exclude self-references (a class names itself throughout its own file)
        navs = [(p, t) for (p, t) in _nav_positions(text, by_simple, by_lower) if t != sid] \
            if text else []
        distinct = [(d, label_of.get(d, "")) for d in dict.fromkeys(out_targets.get(sid, [])) if d != sid]
        for b in scr.buttons:
            dst = _widget_target(b, text, navs, distinct)
            if dst and dst != sid:
                b.target = label_of.get(dst, "")


def _widget_target(b: Widget, text: str, navs: list, distinct: list) -> Optional[str]:
    """distinct: list of (screen_id, label) this screen navigates to."""
    if not navs and not distinct:
        return None
    # 1. android:onClick="handler" -> the handler method's navigation
    if b.onclick and text:
        md = re.search(r"\b" + re.escape(b.onclick) + r"\s*\(", text)
        if md:
            t = _nearest_after(navs, md.start(), 900)
            if t:
                return t
    # 2. the widget's id / view-binding name near an inline navigation
    if b.wid and b.wid != "(unnamed)" and text:
        pats = {"R.id." + b.wid, _camel(b.wid)}
        locs = sorted(m.start() for pat in pats for m in re.finditer(r"\b" + re.escape(pat) + r"\b", text))
        for p in locs:
            t = _nearest_after(navs, p, 700)
            if t:
                return t
    # 3. the button names its destination (Checkout button -> CheckoutActivity)
    tok = (b.wid + " " + b.text).lower()
    for sid, lab in distinct:
        base = re.sub(r"(?:Activity|Fragment)\d*$", "", lab).lower()
        if len(base) >= 4 and base in tok:
            return sid
    # 4. single-destination screen + a primary-looking button -> that destination
    if len(distinct) == 1 and _is_primary(b):
        return distinct[0][0]
    return None


_NAVIGATE = re.compile(r"""navigate\(\s*["']([A-Za-z0-9_\-/{}.:?=&]+)["']""")
_COMPOSABLE = re.compile(r"""composable\(\s*(?:route\s*=\s*)?["']([A-Za-z0-9_\-/{}.:?=&]+)["']""")
_START_DEST = re.compile(r"""startDestination\s*=\s*["']([A-Za-z0-9_\-/{}.:?=&]+)["']""")


# Jetpack Compose signals. jadx renders composables as `void XxxScreen(...Composer...)`
# and widgets as compiled `ButtonKt.Button(...)` / `TextFieldKt.TextField(...)` calls.
_COMPOSE_MARK = re.compile(r"androidx\.compose|\bComposer\b|ComposerKt|SkippableUpdater")
_COMPOSE_SCREEN_FN = re.compile(
    r"\bvoid\s+([A-Z][A-Za-z0-9_]*(?:Screen|Page|Dialog|Route|Content))\s*\(")
_C_BUTTON = re.compile(r"\b([A-Za-z]{0,22}Button)Kt\.")
_C_INPUT = re.compile(r"\b(TextField|OutlinedTextField|BasicTextField|Checkbox|Switch|"
                      r"Slider|RadioButton)Kt\.")


def _compose_screens(f: SourceFile, screens: dict) -> None:
    if not f.first_party or "$" in f.dotted.rsplit(".", 1)[-1]:
        return
    text = f.text
    if not _COMPOSE_MARK.search(text):
        return
    matches = list(_COMPOSE_SCREEN_FN.finditer(text))
    if not matches:
        return
    bounds = [m.start() for m in matches] + [len(text)]
    for i, m in enumerate(matches):
        name = m.group(1)
        body = text[m.start():bounds[i + 1]]
        sid = f"{f.dotted}#{name}"
        if sid in screens:
            continue
        scr = Screen(id=sid, label=name, kind="compose-screen")
        scr.buttons = _agg_widgets(_C_BUTTON, body)
        scr.inputs = _agg_widgets(_C_INPUT, body)
        screens[sid] = scr


def _agg_widgets(rx, body: str) -> list:
    from collections import Counter
    counts: Counter = Counter()
    for mm in rx.finditer(body):
        counts[mm.group(1)] += 1
    return [Widget(wid=(f"×{n}" if n > 1 else ""), kind=kind) for kind, n in counts.most_common(12)]


def _enclosing_screen(screen_fns: list, pos: int):
    cur = None
    for start, sid in screen_fns:
        if start <= pos:
            cur = sid
        else:
            break
    return cur


def _compose_edges(f: SourceFile, screens: dict, edges: list) -> None:
    text = f.text
    if "navigate(" not in text and "composable(" not in text:
        return
    for m in _COMPOSABLE.finditer(text):
        route = _route_label(m.group(1))
        screens.setdefault(f"route:{route}", Screen(id=f"route:{route}", label=route, kind="compose-route"))
    for m in _START_DEST.finditer(text):
        route = _route_label(m.group(1))
        # a graph's start destination is a local root, not the app's launch entry
        screens.setdefault(f"route:{route}", Screen(id=f"route:{route}", label=route, kind="compose-route"))
    # attribute each navigate("x") to the @Composable screen it sits inside
    screen_fns = [(m.start(), f"{f.dotted}#{m.group(1)}") for m in _COMPOSE_SCREEN_FN.finditer(text)]
    file_src = _screen_id_for_file_compose(f, screens)
    for m in _NAVIGATE.finditer(text):
        route = _route_label(m.group(1))
        rid = f"route:{route}"
        screens.setdefault(rid, Screen(id=rid, label=route, kind="compose-route"))
        src = _enclosing_screen(screen_fns, m.start()) or file_src
        if src and src in screens:
            edges.append(Edge(src=src, dst=rid, via="compose"))


def _route_label(raw: str) -> str:
    return raw.split("/")[0].split("?")[0].split("{")[0].strip("/") or raw


def _screen_id_for_file_compose(f: SourceFile, screens: dict) -> Optional[str]:
    if f.dotted in screens:
        return f.dotted
    return None


def _nav_graph_edges(outdir: Optional[str], screens: dict, edges: list) -> None:
    if not outdir:
        return
    for cand in ("resources/res", "res"):
        base = os.path.join(outdir, cand)
        if os.path.isdir(base):
            break
    else:
        return
    for dirpath, _dirs, files in os.walk(base):
        if os.path.basename(dirpath).split("-")[0] != "navigation":
            continue
        for fn in files:
            if fn.endswith(".xml"):
                _parse_nav_graph(os.path.join(dirpath, fn), screens, edges)


def _parse_nav_graph(path: str, screens: dict, edges: list) -> None:
    try:
        root = ET.parse(path).getroot()
    except Exception:  # noqa: BLE001
        return
    # id -> node id/label
    id_to_node: dict[str, str] = {}

    def node_for(el) -> Optional[str]:
        rid = _res_id(el.get(f"{_A}id", ""))
        name = el.get(f"{_A}name", "")
        label = name.rsplit(".", 1)[-1] if name else rid
        if not (rid or name):
            return None
        nid = name or f"dest:{rid}"
        kind = "fragment" if el.tag.rsplit("}", 1)[-1] == "fragment" else "dialog" \
            if el.tag.rsplit("}", 1)[-1] == "dialog" else "activity"
        if nid not in screens:
            screens[nid] = Screen(id=nid, label=label, kind=kind)
        if rid:
            id_to_node[rid] = nid
        return nid

    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag in ("fragment", "activity", "dialog"):
            node_for(el)

    # actions: <action app:destination="@id/x"> nested under a source destination
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag not in ("fragment", "activity", "dialog"):
            continue
        src_id = _res_id(el.get(f"{_A}id", ""))
        src_node = id_to_node.get(src_id)
        for act in el.findall("action"):
            dest = _res_id(act.get(f"{_APP}destination", ""))
            dst_node = id_to_node.get(dest)
            if src_node and dst_node:
                edges.append(Edge(src=src_node, dst=dst_node, via="nav-graph"))
