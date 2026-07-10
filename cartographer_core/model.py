"""Data model for the app map.

Everything the analyzers produce lands in these plain dataclasses, which the
report layer renders to terminal / JSON / HTML. Kept dependency-free so the
model can be json-dumped directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class Component:
    """A manifest-declared component (activity / service / receiver / provider)."""
    name: str                       # fully-qualified class name (real name; survives R8)
    kind: str                       # Activity | Service | Receiver | Provider
    exported: bool = False
    is_launcher: bool = False
    permission: Optional[str] = None
    intent_actions: list[str] = field(default_factory=list)
    deep_link_schemes: list[str] = field(default_factory=list)
    deep_link_hosts: list[str] = field(default_factory=list)
    deep_link_paths: list[str] = field(default_factory=list)


@dataclass
class Screen:
    """A UI destination — an Activity, a Fragment, or a Compose route."""
    id: str                         # canonical node id (class name or route)
    label: str                      # short display label
    kind: str                       # activity | fragment | compose-route | dialog
    layout: Optional[str] = None    # associated R.layout name, if resolved
    is_entry: bool = False          # launcher / start destination
    buttons: list["Widget"] = field(default_factory=list)
    inputs: list["Widget"] = field(default_factory=list)
    exported: bool = False
    purpose: str = "screen"         # inferred role: auth | payment | home | ...
    net_hosts: list[str] = field(default_factory=list)   # backends this screen (or its VM) calls
    uses_perms: list[str] = field(default_factory=list)  # dangerous permissions exercised here


@dataclass
class Widget:
    """An interactive UI element parsed from a layout."""
    wid: str                        # android:id (e.g. btn_login)
    kind: str                       # Button | EditText | ...
    text: str = ""                  # android:text / hint
    onclick: str = ""               # android:onClick handler, if any
    target: str = ""                # screen this control navigates to, if inferred


@dataclass
class Edge:
    """A navigation transition between two screens."""
    src: str                        # source screen id
    dst: str                        # destination screen id / route
    via: str = "intent"             # intent | nav-graph | compose | fragment
    trigger: str = ""               # button/action that triggers it, if known


@dataclass
class Endpoint:
    """A network endpoint the app talks to."""
    method: str                     # GET/POST/... or "URL" for bare literals
    path: str                       # path or full url
    host: str = ""                  # resolved host, if any
    source: str = ""                # where it was found (retrofit iface / literal / webview)
    declared_in: str = ""           # file:line
    called_from: list[str] = field(default_factory=list)  # screen labels inferred to trigger it


@dataclass
class AppMap:
    # identity
    path: str = ""
    package: str = ""
    version_name: str = ""
    version_code: str = ""
    min_sdk: str = ""
    target_sdk: str = ""
    sha256: str = ""
    file_size: int = 0

    framework: str = "Native (Java/Kotlin)"
    framework_note: str = ""
    obfuscated: bool = False
    obfuscation_note: str = ""

    # inventory
    permissions: list[str] = field(default_factory=list)
    dangerous_permissions: list[str] = field(default_factory=list)
    components: list[Component] = field(default_factory=list)

    # UI flow
    screens: list[Screen] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    entry_screens: list[str] = field(default_factory=list)

    # networking
    endpoints: list[Endpoint] = field(default_factory=list)
    base_urls: list[str] = field(default_factory=list)
    network_libs: list[str] = field(default_factory=list)

    # architecture / class map
    total_files: int = 0
    first_party_files: int = 0
    library_files: int = 0
    package_tree: list[dict] = field(default_factory=list)   # [{package, files}]
    roles: dict = field(default_factory=dict)                # role -> count
    tech_stack: list[str] = field(default_factory=list)
    third_party_sdks: list[str] = field(default_factory=list)

    # data / storage behaviour
    storage: list[str] = field(default_factory=list)         # detected persistence mechanisms
    capabilities: list[str] = field(default_factory=list)    # permission-derived device capabilities
    native_libs: list[str] = field(default_factory=list)     # bundled .so names

    # synthesized insight (insight.py)
    summary: str = ""                                        # auto-written plain-English overview
    journeys: list[dict] = field(default_factory=list)       # [{purpose,label,path:[labels]}]
    deep_links: list[dict] = field(default_factory=list)     # [{uri,component,adb}]
    perm_usage: list[dict] = field(default_factory=list)     # [{permission,api,where:[..]}]
    external_entry_points: list[dict] = field(default_factory=list)  # exported reachable surface

    def to_json(self) -> dict:
        return asdict(self)

    def stat_summary(self) -> dict:
        return {
            "screens": len(self.screens),
            "edges": len(self.edges),
            "endpoints": len(self.endpoints),
            "components": len(self.components),
            "first_party_files": self.first_party_files,
            "library_files": self.library_files,
        }
