"""Render the AppMap to terminal, JSON, and a self-contained interactive HTML page.

The HTML is the headline deliverable: an executive summary, an interactive
screen-flow graph (searchable, click a node to jump to its screen), user
journeys, UI<->network bindings, permission-usage attribution, a deep-link
catalog with ready-to-run adb, and the architecture drill-down. Everything is
inlined (no CDN) so it opens offline and shares as one file.
"""

from __future__ import annotations

import html
import json
import os
import re
from collections import defaultdict, deque

from .model import AppMap
from .insight import purpose_meta

_KIND_COLOR = {
    "activity": "#3b82f6",
    "fragment": "#22c55e",
    "compose-route": "#a855f7",
    "compose-screen": "#a855f7",
    "dialog": "#f59e0b",
    "class": "#94a3b8",
}


def _esc(s) -> str:
    return html.escape(str(s), quote=True)


# ===========================================================================
# Terminal
# ===========================================================================
def to_terminal(m: AppMap, log=print) -> None:
    try:
        from rich.console import Console
        from rich.panel import Panel
        from rich.table import Table
    except Exception:  # noqa: BLE001
        return _to_terminal_plain(m, log)

    c = Console()
    ident = (f"[bold]{m.package}[/bold]  v{m.version_name} ({m.version_code})\n"
             f"minSdk {m.min_sdk} / targetSdk {m.target_sdk}   {m.file_size // 1024} KB\n"
             f"Framework: {m.framework}")
    c.print(Panel(ident, title="Android Cartographer", border_style="cyan"))
    if m.framework_note:
        c.print(f"[yellow]⚠ {m.framework_note}[/yellow]")
    if m.obfuscated:
        c.print(f"[yellow]⚠ {m.obfuscation_note}[/yellow]")

    if m.summary:
        c.print(Panel(_strip_md(m.summary), title="What this app is", border_style="green"))

    s = m.stat_summary()
    c.print(f"[bold]Map:[/bold] {s['screens']} screens · {s['edges']} transitions · "
            f"{s['endpoints']} endpoints · {s['components']} components · "
            f"{s['first_party_files']} first-party files\n")

    if m.tech_stack:
        c.print("[bold]Tech stack:[/bold] " + ", ".join(m.tech_stack))
    if m.third_party_sdks:
        c.print("[bold]Third-party SDKs:[/bold] " + ", ".join(m.third_party_sdks))
    if m.storage:
        c.print("[bold]Storage:[/bold] " + ", ".join(m.storage))

    if m.journeys:
        c.print("\n[bold]Key user journeys:[/bold]")
        for j in m.journeys:
            _lbl, icon = purpose_meta(j["purpose"])
            c.print(f"  {icon} " + " [dim]▸[/dim] ".join(j["path"]))

    if m.perm_usage:
        c.print("\n[bold]Permission usage:[/bold]")
        for u in m.perm_usage:
            where = ", ".join(u["where"][:4]) or "(non-first-party code)"
            c.print(f"  [red]{u['permission']}[/red] → {u['api']}  [dim]in {where}[/dim]")

    entry = [sc for sc in m.screens if sc.is_entry]
    if entry:
        c.print("\n[bold]Entry:[/bold] " + ", ".join(sc.label for sc in entry))
    c.print("[bold]Screens:[/bold]")
    out_by = defaultdict(list)
    label = {sc.id: sc.label for sc in m.screens}
    for e in m.edges:
        out_by[e.src].append(e.dst)
    for sc in m.screens[:36]:
        _lbl, icon = purpose_meta(sc.purpose)
        outs = out_by.get(sc.id, [])
        arrow = ("  →  " + ", ".join(sorted({label.get(d, d) for d in outs}))[:70]) if outs else ""
        net = f"  [cyan]🌐 {','.join(sc.net_hosts)}[/cyan]" if sc.net_hosts else ""
        c.print(f"  {icon} [{_KIND_COLOR.get(sc.kind,'white')}]{sc.label}[/]"
                f"[dim] ({sc.kind}{', '+sc.layout if sc.layout else ''})[/dim]{arrow}{net}")
    if len(m.screens) > 36:
        c.print(f"  [dim]... and {len(m.screens)-36} more (see HTML report)[/dim]")

    if m.endpoints:
        by_host = defaultdict(list)
        for ep in m.endpoints:
            by_host[ep.host or "(relative / runtime host)"].append(ep)
        c.print("\n[bold]Network endpoints:[/bold]")
        for host, eps in sorted(by_host.items()):
            c.print(f"  [cyan]{host}[/cyan]  ({len(eps)})")
            for ep in eps[:6]:
                cf = f"  [dim]← {', '.join(ep.called_from)}[/dim]" if ep.called_from else ""
                c.print(f"     [green]{ep.method:7}[/green] {ep.path[:74]}{cf}")

    if m.deep_links:
        c.print("\n[bold]Deep links (openable entry points):[/bold]")
        for d in m.deep_links[:8]:
            c.print(f"  [magenta]{d['uri']}[/magenta]  [dim]→ {d['component']}[/dim]")


def _to_terminal_plain(m: AppMap, log=print) -> None:
    log(f"== {m.package} v{m.version_name} ==")
    if m.summary:
        log(_strip_md(m.summary))
    s = m.stat_summary()
    log(f"{s['screens']} screens, {s['edges']} transitions, {s['endpoints']} endpoints")
    for sc in m.screens[:36]:
        log(f"  {'*' if sc.is_entry else '-'} {sc.label} ({sc.purpose})")


def _strip_md(text: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*", r"\1", text)


# ===========================================================================
# JSON
# ===========================================================================
def to_json_file(m: AppMap, path: str) -> None:
    with open(path, "w") as fh:
        json.dump(m.to_json(), fh, indent=2)


# ===========================================================================
# HTML
# ===========================================================================
def to_html_file(m: AppMap, path: str) -> None:
    with open(path, "w") as fh:
        fh.write(_html(m))


def _search_text(sc) -> str:
    _lbl, _icon = purpose_meta(sc.purpose)
    return " ".join([sc.label, sc.kind, sc.purpose, _lbl, sc.layout or ""]
                    + sc.net_hosts).lower()


# ---- screen-flow SVG --------------------------------------------------------
def _graph_layout(m: AppMap, cap: int = 90):
    id2s = {sc.id: sc for sc in m.screens}
    adj = defaultdict(list)
    radj = defaultdict(list)
    for e in m.edges:
        if e.src in id2s and e.dst in id2s:
            adj[e.src].append(e.dst)
            radj[e.dst].append(e.src)

    entries = [sc.id for sc in m.screens if sc.is_entry]
    if not entries and m.screens:
        entries = [sc.id for sc in m.screens if not radj[sc.id]] or [m.screens[0].id]

    visited, selected = set(), []
    dq = deque(entries)
    while dq and len(selected) < cap:
        n = dq.popleft()
        if n in visited:
            continue
        visited.add(n)
        selected.append(n)
        for t in adj[n]:
            if t not in visited:
                dq.append(t)
    for sc in m.screens:
        if len(selected) >= cap:
            break
        if sc.id not in visited:
            visited.add(sc.id)
            selected.append(sc.id)
    sel = set(selected)
    truncated = len(m.screens) - len(sel)

    dist: dict[str, int] = {}
    roots = [n for n in selected if (id2s[n].is_entry or not radj[n])]
    dq = deque()
    for r in roots:
        dist[r] = 0
        dq.append(r)
    while dq:
        n = dq.popleft()
        for t in adj[n]:
            if t in sel and t not in dist:
                dist[t] = dist[n] + 1
                dq.append(t)
    for nid in selected:
        dist.setdefault(nid, (max(dist.values()) + 1) if dist else 0)

    layers: dict[int, list[str]] = defaultdict(list)
    for nid in selected:
        layers[dist[nid]].append(nid)

    MAX_PER_ROW = 8
    visual_rows: list[list[str]] = []
    for layer in sorted(layers):
        row = sorted(layers[layer], key=lambda x: id2s[x].label.lower())
        for i in range(0, len(row), MAX_PER_ROW):
            visual_rows.append(row[i:i + MAX_PER_ROW])

    node_w, node_h, gap_x, gap_y, pad = 176, 42, 30, 74, 30
    widest = max((len(r) for r in visual_rows), default=1)
    width = pad * 2 + widest * (node_w + gap_x)
    positions: dict[str, tuple[int, int]] = {}
    for ri, row in enumerate(visual_rows):
        row_w = len(row) * (node_w + gap_x) - gap_x
        x0 = (width - row_w) // 2
        y = pad + ri * (node_h + gap_y)
        for i, nid in enumerate(row):
            positions[nid] = (x0 + i * (node_w + gap_x), y)
    height = pad * 2 + len(visual_rows) * (node_h + gap_y)

    edges = [(e.src, e.dst, dist[e.src] >= dist[e.dst])
             for e in m.edges if e.src in sel and e.dst in sel]
    return id2s, positions, edges, node_w, node_h, width, height, truncated


def _svg_graph(m: AppMap, sid_index: dict) -> str:
    if not m.screens:
        return "<p class='muted'>No screens resolved (manifest-only / native app).</p>"
    id2s, pos, edges, nw, nh, W, H, trunc = _graph_layout(m)
    parts = [f'<svg viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
             f'xmlns="http://www.w3.org/2000/svg" class="flow">',
             '<defs>'
             '<marker id="arw" markerWidth="9" markerHeight="9" refX="8" refY="3" orient="auto" '
             'markerUnits="strokeWidth"><path d="M0,0 L8,3 L0,6 Z" fill="#64748b"/></marker>'
             '<marker id="arwb" markerWidth="9" markerHeight="9" refX="8" refY="3" orient="auto" '
             'markerUnits="strokeWidth"><path d="M0,0 L8,3 L0,6 Z" fill="#f59e0b"/></marker></defs>']

    for src, dst, back in edges:
        if src not in pos or dst not in pos:
            continue
        x1, y1 = pos[src]; x2, y2 = pos[dst]
        sx, sy = x1 + nw // 2, y1 + nh
        tx, ty = x2 + nw // 2, y2
        if back:
            sy, ty = y1, y2 + nh
        my = (sy + ty) / 2
        color = "#f59e0b" if back else "#64748b"
        mark = "arwb" if back else "arw"
        dash = ' stroke-dasharray="5,4"' if back else ""
        parts.append(
            f'<path class="edge e-{sid_index[src]} e-{sid_index[dst]}" '
            f'd="M{sx},{sy} C{sx},{my} {tx},{my} {tx},{ty}" fill="none" stroke="{color}" '
            f'stroke-width="1.6" opacity="0.5"{dash} marker-end="url(#{mark})"/>')

    for nid, (x, y) in pos.items():
        sc = id2s[nid]
        i = sid_index[nid]
        col = _KIND_COLOR.get(sc.kind, "#94a3b8")
        stroke = "#fbbf24" if sc.is_entry else col
        sw = 3 if sc.is_entry else 1.5
        _lbl, icon = purpose_meta(sc.purpose)
        label = _esc(sc.label[:20] + ("…" if len(sc.label) > 20 else ""))
        sub = _esc((sc.layout or _lbl)[:24])
        star = "★ " if sc.is_entry else ""
        net = '<circle cx="{cx}" cy="{cy}" r="4" fill="#38bdf8"/>'.format(cx=x + nw - 12, cy=y + 12) \
            if sc.net_hosts else ""
        parts.append(
            f'<g class="node n-{i}" data-i="{i}" data-card="sc-{i}" data-search="{_esc(_search_text(sc))}">'
            f'<rect x="{x}" y="{y}" width="{nw}" height="{nh}" rx="8" fill="#0f172a" '
            f'stroke="{stroke}" stroke-width="{sw}"/>'
            f'<rect x="{x}" y="{y}" width="5" height="{nh}" rx="2" fill="{col}"/>'
            f'<text x="{x+15}" y="{y+18}" fill="#e2e8f0" font-size="12.5" font-weight="600">'
            f'{star}{icon} {label}</text>'
            f'<text x="{x+15}" y="{y+33}" fill="#64748b" font-size="10.5">{sub}</text>'
            f'{net}</g>')
    parts.append("</svg>")
    note = (f'<p class="muted">Graph shows {len(pos)} of {len(m.screens)} screens '
            f'({trunc} more omitted for readability — all are listed below).</p>' if trunc else "")
    return "".join(parts) + note


def _chips(items, cls="chip") -> str:
    return "".join(f'<span class="{cls}">{_esc(i)}</span>' for i in items)


def _screen_cards(m: AppMap, sid_index: dict) -> str:
    out_by = defaultdict(list)
    label = {sc.id: sc.label for sc in m.screens}
    for e in m.edges:
        out_by[e.src].append((label.get(e.dst, e.dst), e.via))
    cards = []
    for sc in m.screens:
        i = sid_index[sc.id]
        plabel, icon = purpose_meta(sc.purpose)
        outs = sorted(set(out_by.get(sc.id, [])))
        meta = _chips([f"{icon} {plabel}"], "chip purpose") + _chips(
            [sc.kind] + ([sc.layout] if sc.layout else []) +
            (["exported"] if sc.exported else []) + (["★ entry"] if sc.is_entry else []), "chip sm")
        net = _chips([f"🌐 {h}" for h in sc.net_hosts], "chip net") if sc.net_hosts else ""
        perm = _chips(sc.uses_perms, "chip warn-chip") if sc.uses_perms else ""
        btns = "".join(
            f'<li><b>{_esc(b.kind)}</b> <code>{_esc(b.wid)}</code>'
            + (f' — “{_esc(b.text)}”' if b.text else "")
            + (f' <span class="wtarget">→ {_esc(b.target)}</span>' if b.target else "")
            + (f' <span class="muted">onClick={_esc(b.onclick)}</span>' if b.onclick else "")
            + "</li>" for b in sc.buttons)
        inps = "".join(
            f'<li><b>{_esc(x.kind)}</b> <code>{_esc(x.wid)}</code>'
            + (f' — “{_esc(x.text)}”' if x.text else "") + "</li>" for x in sc.inputs)
        nav = "".join(f'<span class="chip nav" data-jump="{_esc(d)}">{_esc(d)} '
                      f'<span class="via">{_esc(v)}</span></span>' for d, v in outs)
        body = ""
        if net or perm:
            body += f'<div class="col"><div class="chips">{net}{perm}</div></div>'
        if btns:
            body += f'<div class="col"><h5>Buttons ({len(sc.buttons)})</h5><ul>{btns}</ul></div>'
        if inps:
            body += f'<div class="col"><h5>Inputs ({len(sc.inputs)})</h5><ul>{inps}</ul></div>'
        if nav:
            body += f'<div class="col"><h5>Navigates to</h5><div class="chips">{nav}</div></div>'
        if not body:
            body = '<div class="muted">No widgets or transitions resolved for this screen.</div>'
        dot = _KIND_COLOR.get(sc.kind, "#94a3b8")
        cards.append(
            f'<div class="scard" id="sc-{i}" data-i="{i}" data-search="{_esc(_search_text(sc))}">'
            f'<div class="scard-h"><span class="dot" style="background:{dot}"></span>'
            f'<span class="stitle">{icon} {_esc(sc.label)}</span>{meta}</div>'
            f'<div class="scard-b">{body}</div></div>')
    return "".join(cards)


def _journeys_html(m: AppMap) -> str:
    if not m.journeys:
        return ""
    rows = []
    for j in m.journeys:
        _l, icon = purpose_meta(j["purpose"])
        steps = []
        for lab, pp in zip(j["path"], j["path_purposes"]):
            _pl, pic = purpose_meta(pp)
            steps.append(f'<span class="jstep">{pic} {_esc(lab)}</span>')
        rows.append(f'<div class="journey"><span class="jgoal">{icon} {_esc(j["label"])}</span>'
                    f'<div class="jpath">{" <span class=arr>▸</span> ".join(steps)}</div></div>')
    return ('<h2>User journeys <span class="muted">(shortest path from launch, inferred)</span></h2>'
            f'<div class="journeys">{"".join(rows)}</div>')


def _network_section(m: AppMap) -> str:
    if not m.endpoints and not m.base_urls:
        return '<p class="muted">No network endpoints resolved from static strings.</p>'
    by_host = defaultdict(list)
    for ep in m.endpoints:
        by_host[ep.host or "(relative path / host set at runtime)"].append(ep)
    blocks = []
    if m.base_urls:
        blocks.append('<div class="hostblock"><h4>Base URLs</h4>' +
                      "".join(f'<div class="ep"><code>{_esc(u)}</code></div>' for u in m.base_urls) + "</div>")
    for host, eps in sorted(by_host.items()):
        rows = "".join(
            f'<div class="ep"><span class="method m-{_esc(ep.method.lower())}">{_esc(ep.method)}</span>'
            f'<code>{_esc(ep.path)}</code>'
            + (f'<span class="calledfrom">← {_esc(", ".join(ep.called_from))}</span>' if ep.called_from else "")
            + f'<span class="muted src">{_esc(ep.declared_in)}</span></div>' for ep in eps[:60])
        more = f'<div class="muted">… {len(eps)-60} more</div>' if len(eps) > 60 else ""
        blocks.append(f'<div class="hostblock"><h4>{_esc(host)} '
                      f'<span class="muted">({len(eps)})</span></h4>{rows}{more}</div>')
    return "".join(blocks)


def _perm_usage_html(m: AppMap) -> str:
    if not m.perm_usage:
        return ""
    rows = []
    for u in m.perm_usage:
        where = ", ".join(u["where"]) or '<span class="muted">library / obfuscated code</span>'
        sig = ", ".join(u["signals"])
        rows.append(f'<tr><td><code>{_esc(u["permission"])}</code></td><td>{_esc(u["api"])}</td>'
                    f'<td>{where}</td><td class="muted">{_esc(sig)}</td></tr>')
    return ('<h2>Permission usage <span class="muted">(where a sensitive permission is actually '
            'exercised)</span></h2>'
            '<table class="tbl"><thead><tr><th>Permission</th><th>Capability</th>'
            '<th>Used in</th><th>API signal</th></tr></thead><tbody>' + "".join(rows) + "</tbody></table>")


def _deeplinks_html(m: AppMap) -> str:
    out = ""
    if m.deep_links:
        rows = "".join(
            f'<div class="dl"><code class="uri">{_esc(d["uri"])}</code>'
            f'<span class="muted">→ {_esc(d["component"])}</span>'
            f'<button class="copy" data-cmd="{_esc(d["adb"])}">copy adb</button></div>'
            for d in m.deep_links)
        out += ('<h2>Deep links <span class="muted">(external entry points — click to copy an adb '
                'launch)</span></h2><div class="dls">' + rows + "</div>")
    if m.external_entry_points:
        rows = "".join(
            f'<tr><td>{_esc(e["kind"])}</td><td><code>{_esc(e["component"])}</code></td>'
            f'<td>{_esc(e["guard"])}</td>'
            f'<td>{_esc(", ".join(e["actions"]) + (" " + " ".join(e["schemes"]) if e["schemes"] else ""))}</td></tr>'
            for e in m.external_entry_points)
        out += ('<h4>Other exported entry points</h4>'
                '<table class="tbl"><thead><tr><th>Type</th><th>Component</th><th>Guard</th>'
                '<th>Reachable via</th></tr></thead><tbody>' + rows + "</tbody></table>")
    return out


def _bars(m: AppMap) -> str:
    if not m.package_tree:
        return ""
    mx = max(p["files"] for p in m.package_tree) or 1
    rows = "".join(
        f'<div class="barrow"><span class="bpkg" title="{_esc(p["package"])}">{_esc(p["package"])}</span>'
        f'<span class="bar"><span style="width:{int(100*p["files"]/mx)}%"></span></span>'
        f'<span class="bn">{p["files"]}</span></div>' for p in m.package_tree)
    return f'<div class="bars">{rows}</div>'


def _components_table(m: AppMap) -> str:
    rows = []
    for c in sorted(m.components, key=lambda x: (x.kind, x.name)):
        flags = [f for f, on in (("launcher", c.is_launcher), ("exported", c.exported),
                                 ("perm-guarded", bool(c.permission))) if on]
        dl = ", ".join(s + "://" for s in c.deep_link_schemes)
        rows.append(
            f'<tr><td>{_esc(c.kind)}</td><td><code>{_esc(c.name)}</code></td>'
            f'<td>{_esc(", ".join(flags))}</td>'
            f'<td>{_esc(", ".join(a.rsplit(".",1)[-1] for a in c.intent_actions))}</td>'
            f'<td>{_esc(dl)}</td></tr>')
    return ("<table class='tbl'><thead><tr><th>Type</th><th>Class</th><th>Flags</th>"
            "<th>Actions</th><th>Deep links</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>")


def _summary_html(m: AppMap) -> str:
    if not m.summary:
        return ""
    txt = _esc(m.summary)
    txt = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", txt)
    return f'<div class="summary"><h3>Executive summary</h3><p>{txt}</p></div>'


def _html(m: AppMap) -> str:
    sid_index = {sc.id: i for i, sc in enumerate(m.screens)}
    warn = ""
    if m.framework_note:
        warn += f'<div class="warn">⚠ {_esc(m.framework_note)}</div>'
    if m.obfuscated:
        warn += f'<div class="warn">⚠ {_esc(m.obfuscation_note)}</div>'

    compose_note = ""
    if "Jetpack Compose" in m.tech_stack:
        n_comp = sum(1 for sc in m.screens if sc.kind in ("compose-screen", "compose-route"))
        compose_note = (
            '<div class="warn">This is a <b>Jetpack Compose</b> app — screens are '
            f'<code>@Composable</code> functions ({n_comp} detected, shown as purple nodes), not XML '
            'layouts. Their widgets are read from compiled Compose calls. Compose navigation is '
            'callback/string-route based, so some transitions are only partially recoverable from a '
            'static decompile and the graph may show screens with few edges.</div>')

    s = m.stat_summary()
    stat_cards = "".join(
        f'<div class="stat"><div class="sv">{v}</div><div class="sl">{_esc(k)}</div></div>'
        for k, v in [("screens", s["screens"]), ("transitions", s["edges"]),
                     ("endpoints", s["endpoints"]), ("components", s["components"]),
                     ("first-party files", s["first_party_files"]),
                     ("library files", s["library_files"])])
    roles = "".join(f'<div class="barrow"><span class="bpkg">{_esc(r)}</span>'
                    f'<span class="bn">{n}</span></div>' for r, n in m.roles.items())
    perms = "".join(f'<li><code>{_esc(p)}</code></li>' for p in m.permissions)
    dperms = _chips([p.rsplit(".", 1)[-1] for p in m.dangerous_permissions], "chip warn-chip")

    return _TEMPLATE.format(
        pkg=_esc(m.package or "unknown"),
        ver=_esc(f"v{m.version_name} ({m.version_code})"),
        sdk=_esc(f"minSdk {m.min_sdk} · targetSdk {m.target_sdk}"),
        size=f"{m.file_size // 1024} KB", framework=_esc(m.framework),
        sha=_esc(m.sha256[:32]), warn=warn, summary=_summary_html(m),
        stack=_chips(m.tech_stack) or '<span class="muted">not detected</span>',
        sdks=_chips(m.third_party_sdks, "chip sdk") or '<span class="muted">none detected</span>',
        storage=_chips(m.storage) or '<span class="muted">none detected</span>',
        caps=_chips(m.capabilities, "chip warn-chip") or '<span class="muted">no sensitive device access</span>',
        netlibs=_chips(m.network_libs) or '<span class="muted">none detected</span>',
        natlibs=(_chips(m.native_libs[:24]) if m.native_libs else '<span class="muted">none</span>'),
        statcards=stat_cards, compose_note=compose_note, journeys=_journeys_html(m),
        svg=_svg_graph(m, sid_index), screens=_screen_cards(m, sid_index),
        network=_network_section(m), permusage=_perm_usage_html(m), deeplinks=_deeplinks_html(m),
        bars=_bars(m), roles=roles or '<span class="muted">n/a</span>',
        components=_components_table(m), ncomp=len(m.components),
        dperms=dperms or '<span class="muted">none</span>',
        perms=perms or "<li class='muted'>none</li>")


_TEMPLATE = r"""<style>
:root{{--bg:#0b1120;--panel:#111a2e;--panel2:#0f172a;--line:#1e293b;--tx:#e2e8f0;--mut:#64748b;--acc:#38bdf8;}}
*{{box-sizing:border-box;}}
body{{margin:0;background:var(--bg);color:var(--tx);font:14px/1.55 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;}}
.wrap{{max-width:1200px;margin:0 auto;padding:28px 20px 90px;}}
h1{{font-size:22px;margin:0 0 2px;}} h2{{font-size:16px;margin:36px 0 12px;color:var(--acc);border-bottom:1px solid var(--line);padding-bottom:6px;}}
h3{{font-size:14px;margin:0 0 8px;}} h4{{margin:14px 0 6px;font-size:13px;}} h5{{margin:0 0 4px;font-size:12px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em;}}
code{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:#93c5fd;word-break:break-all;}}
.muted,.src{{color:var(--mut);font-weight:400;}}
.hdr{{display:flex;flex-wrap:wrap;gap:16px;align-items:baseline;justify-content:space-between;}}
.hdr .meta{{color:var(--mut);font-size:12.5px;}}
.warn{{background:#3b2a08;border:1px solid #a16207;color:#fde68a;padding:10px 14px;border-radius:8px;margin:14px 0;font-size:13px;}}
.summary{{background:linear-gradient(135deg,#0e1c2f,#131a2e);border:1px solid #24406b;border-radius:12px;padding:16px 20px;margin:18px 0;}}
.summary p{{margin:0;font-size:14.5px;line-height:1.65;}} .summary b{{color:#fff;}}
.chips{{display:flex;flex-wrap:wrap;gap:6px;}}
.chip{{display:inline-block;background:#172033;border:1px solid var(--line);border-radius:999px;padding:3px 10px;font-size:12px;}}
.chip.sm{{padding:1px 8px;font-size:11px;color:var(--mut);}}
.chip.purpose{{background:#101f38;border-color:#26426e;color:#bfdbfe;}}
.chip.sdk{{background:#0e2a1e;border-color:#14532d;color:#86efac;}}
.chip.net{{background:#07253a;border-color:#0e5a86;color:#7dd3fc;}}
.chip.warn-chip{{background:#3b1d1d;border-color:#7f1d1d;color:#fca5a5;}}
.chip.nav{{background:#101c33;border-color:#1d3355;cursor:pointer;}} .chip.nav:hover{{border-color:var(--acc);}}
.chip.nav .via{{color:var(--mut);font-size:10px;margin-left:4px;}}
.wtarget{{color:#7dd3fc;font-size:11.5px;font-weight:600;}}
.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:22px;}}
@media(max-width:760px){{.grid2{{grid-template-columns:1fr;}}}}
.stats{{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0;}}
.stat{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 18px;min-width:108px;}}
.stat .sv{{font-size:24px;font-weight:700;color:#fff;}} .stat .sl{{font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em;}}
.journeys{{display:flex;flex-direction:column;gap:8px;}}
.journey{{display:flex;gap:14px;align-items:center;flex-wrap:wrap;background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:8px 14px;}}
.jgoal{{font-weight:600;min-width:150px;}}
.jpath{{display:flex;flex-wrap:wrap;gap:6px;align-items:center;color:#cbd5e1;font-size:13px;}}
.jstep{{background:#0f1a2e;border:1px solid var(--line);border-radius:6px;padding:2px 8px;}} .arr{{color:var(--mut);}}
.toolbar{{display:flex;gap:12px;align-items:center;margin:6px 0 10px;}}
#screenSearch{{flex:1;max-width:340px;background:var(--panel2);border:1px solid var(--line);border-radius:8px;color:var(--tx);padding:8px 12px;font-size:13px;}}
.legend{{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--mut);}}
.legend i{{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:5px;vertical-align:middle;}}
.flowbox{{overflow:auto;max-height:75vh;background:var(--panel2);border:1px solid var(--line);border-radius:12px;padding:10px;}}
svg.flow text{{font-family:inherit;}} svg.flow .node{{cursor:pointer;transition:opacity .15s;}}
.scards{{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:14px;}}
.scard{{background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden;scroll-margin-top:16px;}}
.scard.flash{{animation:fl 1.4s ease;}} @keyframes fl{{0%,40%{{box-shadow:0 0 0 2px var(--acc);border-color:var(--acc);}}100%{{box-shadow:none;}}}}
.scard.hide{{display:none;}}
.scard-h{{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:10px 12px;background:#0d1526;border-bottom:1px solid var(--line);}}
.scard-h .stitle{{font-weight:600;}} .dot{{width:9px;height:9px;border-radius:50%;flex:none;}}
.scard-b{{padding:10px 12px;display:flex;flex-direction:column;gap:10px;}}
.scard-b ul{{margin:0;padding-left:16px;}} .scard-b li{{margin:2px 0;font-size:12.5px;}}
.hostblock{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:8px 14px;margin-bottom:12px;}}
.ep{{display:flex;align-items:center;gap:10px;padding:4px 0;border-top:1px solid #16233c;flex-wrap:wrap;}} .ep:first-of-type{{border-top:none;}}
.ep .src{{margin-left:auto;font-size:11px;}} .calledfrom{{color:#7dd3fc;font-size:11.5px;}}
.method{{font-size:11px;font-weight:700;border-radius:4px;padding:1px 7px;min-width:60px;text-align:center;background:#1e293b;}}
.m-get{{color:#86efac;}} .m-post{{color:#fdba74;}} .m-put{{color:#93c5fd;}} .m-delete{{color:#fca5a5;}}
.m-webview{{color:#c4b5fd;}} .m-url{{color:#94a3b8;}} .m-patch{{color:#f9a8d4;}}
.dls{{display:flex;flex-direction:column;gap:6px;}}
.dl{{display:flex;align-items:center;gap:10px;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:6px 12px;flex-wrap:wrap;}}
.dl .uri{{color:#e9d5ff;}} .copy{{margin-left:auto;background:#1e293b;color:var(--acc);border:1px solid var(--line);border-radius:6px;padding:3px 10px;font-size:11px;cursor:pointer;}}
.copy:hover{{border-color:var(--acc);}} .copy.done{{color:#86efac;}}
.bars{{display:flex;flex-direction:column;gap:5px;}}
.barrow{{display:flex;align-items:center;gap:10px;font-size:12px;}}
.bpkg{{flex:0 0 46%;color:#cbd5e1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}}
.bar{{flex:1;height:8px;background:#0f172a;border-radius:4px;overflow:hidden;}} .bar span{{display:block;height:100%;background:linear-gradient(90deg,#0ea5e9,#6366f1);}}
.bn{{flex:none;color:var(--mut);width:34px;text-align:right;}}
.tbl{{width:100%;border-collapse:collapse;font-size:12.5px;}}
.tbl th,.tbl td{{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top;}}
.tbl th{{color:var(--mut);font-weight:600;}}
.permcols{{columns:2;font-size:12px;}} .permcols li{{margin:2px 0;}}
</style>
<div class="wrap">
  <div class="hdr"><div><h1>{pkg}</h1>
    <div class="meta">{ver} · {sdk} · {size} · {framework}<br><code>sha256:{sha}…</code></div></div></div>
  {warn}
  {summary}
  <div class="stats">{statcards}</div>
  <div class="chips" style="margin:8px 0;">{stack}</div>
  <div class="grid2" style="margin-top:14px;">
    <div><h5>Third-party SDKs</h5><div class="chips">{sdks}</div></div>
    <div><h5>Local storage</h5><div class="chips">{storage}</div></div>
    <div><h5>Device access (from permissions)</h5><div class="chips">{caps}</div></div>
    <div><h5>Network stacks</h5><div class="chips">{netlibs}</div></div>
    <div><h5>Native libraries</h5><div class="chips">{natlibs}</div></div>
  </div>

  {journeys}

  <h2>Screen flow</h2>
  {compose_note}
  <div class="toolbar">
    <input id="screenSearch" type="text" placeholder="filter screens (name, purpose, host)…" />
    <div class="legend">
      <span><i style="background:#3b82f6"></i>Activity</span>
      <span><i style="background:#22c55e"></i>Fragment</span>
      <span><i style="background:#a855f7"></i>Compose screen/route</span>
      <span><i style="background:#fbbf24"></i>★ entry</span>
      <span><i style="background:#38bdf8;border-radius:50%"></i>network</span>
      <span style="color:#f59e0b">– – back/loop</span>
    </div>
  </div>
  <div class="flowbox">{svg}</div>

  <h2>Screens &amp; widgets</h2>
  <div class="scards">{screens}</div>

  <h2>Network surface</h2>
  {network}

  {permusage}

  {deeplinks}

  <h2>Architecture</h2>
  <div class="grid2">
    <div><h4>First-party packages (by file count)</h4>{bars}</div>
    <div><h4>Classes by role</h4><div class="bars">{roles}</div></div>
  </div>

  <h2>Components ({ncomp})</h2>
  {components}

  <h2>Permissions</h2>
  <div style="margin-bottom:10px;">{dperms}</div>
  <ul class="permcols">{perms}</ul>
</div>
<script>
(function(){{
  var nodes=document.querySelectorAll('svg.flow .node');
  var edges=document.querySelectorAll('svg.flow .edge');
  function dimAll(v){{nodes.forEach(function(o){{o.style.opacity=v;}});}}
  nodes.forEach(function(n){{
    var i=n.getAttribute('data-i');
    n.addEventListener('mouseenter',function(){{
      dimAll(0.28); n.style.opacity=1;
      edges.forEach(function(e){{
        if(e.classList.contains('e-'+i)){{e.style.opacity=0.95;e.setAttribute('stroke-width','2.6');}}
        else{{e.style.opacity=0.05;}}
      }});
    }});
    n.addEventListener('mouseleave',function(){{
      dimAll(1); edges.forEach(function(e){{e.style.opacity=0.5;e.setAttribute('stroke-width','1.6');}});
    }});
    n.addEventListener('click',function(){{
      var card=document.getElementById(n.getAttribute('data-card'));
      if(card){{card.scrollIntoView({{behavior:'smooth',block:'center'}});
        card.classList.remove('flash'); void card.offsetWidth; card.classList.add('flash');}}
    }});
  }});
  var box=document.getElementById('screenSearch');
  box.addEventListener('input',function(){{
    var q=box.value.trim().toLowerCase();
    document.querySelectorAll('.scard').forEach(function(c){{
      var hit=!q||(c.getAttribute('data-search')||'').indexOf(q)>=0;
      c.classList.toggle('hide',!hit);
    }});
    nodes.forEach(function(n){{
      var hit=!q||(n.getAttribute('data-search')||'').indexOf(q)>=0;
      n.style.opacity=hit?1:0.12;
    }});
  }});
  document.querySelectorAll('.chip.nav[data-jump]').forEach(function(ch){{
    ch.addEventListener('click',function(){{
      var name=ch.getAttribute('data-jump');
      var cards=document.querySelectorAll('.scard');
      for(var k=0;k<cards.length;k++){{
        var t=cards[k].querySelector('.stitle');
        if(t&&t.textContent.indexOf(name)>=0){{
          cards[k].scrollIntoView({{behavior:'smooth',block:'center'}});
          cards[k].classList.remove('flash'); void cards[k].offsetWidth; cards[k].classList.add('flash'); break;}}
      }}
    }});
  }});
  document.querySelectorAll('.copy').forEach(function(b){{
    b.addEventListener('click',function(){{
      var cmd=b.getAttribute('data-cmd');
      navigator.clipboard&&navigator.clipboard.writeText(cmd);
      var o=b.textContent; b.textContent='copied ✓'; b.classList.add('done');
      setTimeout(function(){{b.textContent=o;b.classList.remove('done');}},1200);
    }});
  }});
}})();
</script>
"""
