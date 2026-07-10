"""Synthesis pass: turn the raw map into understanding.

Runs after the structural analyzers and derives the higher-level reading a human
would form: what each screen is *for*, which screens hit the network, which
screens actually exercise a dangerous permission, the notable user journeys, the
external (deep-link) entry points, and a plain-English executive summary.

Everything here is inference over static signals and is labelled as such — it is
meant to orient a reviewer quickly, not to be treated as ground truth.
"""

from __future__ import annotations

import re
from collections import defaultdict, deque

from .model import AppMap, Screen
from .scan import SourceIndex

# ---------------------------------------------------------------------------
# Screen purpose classification
# ---------------------------------------------------------------------------
# (key, label, icon, keyword list) — first match wins, so order = priority.
PURPOSES = [
    ("auth", "Auth", "\U0001F510",
     ["login", "signin", "sign_in", "signup", "sign_up", "register", "auth",
      "otp", "password", "passcode", "forgot", "verify", "verification", "credential",
      "biometric", "fingerprint", "2fa", "mfa"]),
    ("payment", "Payment", "\U0001F4B3",
     ["payment", "checkout", "billing", "card", "subscribe", "subscription",
      "premium", "purchase", "upgrade", "plan", "pricing", "paywall", "wallet",
      "stripe", "order", "cart", "invoice", "refund"]),
    ("onboarding", "Onboarding", "\U0001F44B",
     ["splash", "onboard", "intro", "welcome", "walkthrough", "tutorial",
      "getstarted", "get_started", "landing"]),
    ("home", "Home", "\U0001F3E0",
     ["home", "dashboard", "main", "feed", "explore", "discover", "principal"]),
    ("settings", "Settings", "⚙️",
     ["setting", "preference", "config", "options", "ajustes", "prefs"]),
    ("profile", "Profile", "\U0001F464",
     ["profile", "account", "member", "perfil"]),
    ("chat", "Chat", "\U0001F4AC",
     ["chat", "message", "conversation", "inbox", "messenger"]),
    ("media", "Media / Camera", "\U0001F3AC",
     ["camera", "gallery", "photo", "video", "player", "media", "record",
      "scan", "scanner", "qr", "barcode", "capture"]),
    ("map", "Map / Location", "\U0001F5FA️",
     ["map", "location", "navigation", "route", "gps", "tracking"]),
    ("search", "Search", "\U0001F50E",
     ["search", "filter", "query", "buscar"]),
    ("web", "WebView", "\U0001F310",
     ["web", "browser", "webview"]),
    ("notification", "Notifications", "\U0001F514",
     ["notification", "alert", "alarm", "reminder", "alerta"]),
    ("list", "List / Detail", "\U0001F4CB",
     ["list", "detail", "item", "catalog", "products", "history", "detalle"]),
    ("help", "Help / Legal", "ℹ️",
     ["help", "support", "about", "faq", "terms", "privacy", "legal", "contact"]),
]
_PURPOSE_META = {k: (label, icon) for k, label, icon, _ in PURPOSES}
_PURPOSE_META["screen"] = ("Screen", "▫️")


def purpose_meta(key: str) -> tuple[str, str]:
    return _PURPOSE_META.get(key, _PURPOSE_META["screen"])


def _classify(scr: Screen) -> str:
    hay = " ".join([scr.label, scr.layout or "", scr.id.rsplit(".", 1)[-1]]
                   + [w.text for w in scr.buttons] + [w.text for w in scr.inputs]).lower()
    for key, _label, _icon, kws in PURPOSES:
        if any(k in hay for k in kws):
            return key
    return "screen"


# ---------------------------------------------------------------------------
# Name-affinity feature token (for binding screens<->classes without a call graph)
# ---------------------------------------------------------------------------
_SUFFIXES = ("Activity", "Fragment", "ViewModel", "Repository", "Repo", "Presenter",
             "Controller", "Service", "Api", "ApiService", "Client", "Manager",
             "UseCase", "Interactor", "Impl", "Screen", "DataSource", "Store")


def _feature_token(class_simple: str) -> str:
    name = class_simple.split("$", 1)[0]
    for suf in sorted(_SUFFIXES, key=len, reverse=True):
        if name.endswith(suf) and len(name) > len(suf):
            name = name[: -len(suf)]
            break
    return name.lower()


def _class_from_loc(declared_in: str) -> str:
    path = declared_in.split(":", 1)[0]
    base = path.rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[0]


# ---------------------------------------------------------------------------
# Permission -> API signatures, for usage attribution
# ---------------------------------------------------------------------------
_PERM_APIS = {
    "CAMERA": ("Camera", ["androidx.camera", "Camera2", "CameraManager", "CameraDevice",
                          "android.hardware.Camera", "takePicture", "ImageCapture"]),
    "ACCESS_FINE_LOCATION": ("Location", ["FusedLocationProviderClient", "LocationManager",
                                          "getLastLocation", "requestLocationUpdates", "LocationRequest"]),
    "ACCESS_COARSE_LOCATION": ("Location", ["FusedLocationProviderClient", "LocationManager",
                                            "requestLocationUpdates"]),
    "RECORD_AUDIO": ("Microphone", ["MediaRecorder", "AudioRecord", "startRecording"]),
    "READ_CONTACTS": ("Contacts", ["ContactsContract", "CommonDataKinds"]),
    "READ_SMS": ("SMS", ["SmsManager", "Telephony", "content://sms"]),
    "RECEIVE_SMS": ("SMS", ["SmsRetriever", "SMS_RECEIVED", "Telephony"]),
    "READ_EXTERNAL_STORAGE": ("Storage", ["MediaStore", "getExternalStorage", "Environment.getExternal"]),
    "READ_MEDIA_IMAGES": ("Media library", ["MediaStore.Images", "MediaStore"]),
    "BODY_SENSORS": ("Body sensors", ["SensorManager", "TYPE_HEART_RATE", "Sensor.TYPE"]),
    "ACTIVITY_RECOGNITION": ("Activity recognition", ["ActivityRecognition", "DetectedActivity"]),
    "POST_NOTIFICATIONS": ("Notifications", ["NotificationManager", "NotificationCompat", "NotificationChannel"]),
    "BLUETOOTH_CONNECT": ("Bluetooth", ["BluetoothAdapter", "BluetoothGatt", "BluetoothDevice"]),
    "NFC": ("NFC", ["NfcAdapter", "Ndef"]),
    "READ_CALENDAR": ("Calendar", ["CalendarContract"]),
    "READ_PHONE_STATE": ("Phone state", ["TelephonyManager", "getDeviceId", "getImei"]),
}


def build(m: AppMap, idx: SourceIndex, log=print) -> None:
    # 1. Classify every screen's purpose.
    for scr in m.screens:
        scr.purpose = _classify(scr)

    # 2. Bind screens <-> endpoints and permission usage via name affinity + file map.
    _bind_network(m)
    _attribute_permissions(m, idx)

    # 3. Notable user journeys.
    m.journeys = _journeys(m)

    # 4. External entry points (deep links) with ready-to-run adb.
    m.deep_links = _deep_link_catalog(m)
    m.external_entry_points = _external_surface(m)

    # 5. Executive summary.
    m.summary = _summary(m)
    log(f"[*] Synthesized summary, {len(m.journeys)} journey(s), "
        f"{len(m.deep_links)} deep link(s), {len(m.perm_usage)} permission-usage mapping(s).")


# ---------------------------------------------------------------------------
def _bind_network(m: AppMap) -> None:
    # screen feature token -> screen
    tok2screen: dict[str, Screen] = {}
    for scr in m.screens:
        tok = _feature_token(scr.label)
        if len(tok) >= 4:
            tok2screen.setdefault(tok, scr)
    # dotted/simple class -> screen (for direct file matches, e.g. WebView in an Activity)
    simple2screen = {scr.id.rsplit(".", 1)[-1]: scr for scr in m.screens}

    for ep in m.endpoints:
        cls = _class_from_loc(ep.declared_in)
        bound: list[Screen] = []
        if cls in simple2screen:                       # declared right in a screen (WebView/URL)
            bound.append(simple2screen[cls])
        tok = _feature_token(cls)
        if len(tok) >= 4 and tok in tok2screen:        # LoginViewModel -> LoginActivity
            s = tok2screen[tok]
            if s not in bound:
                bound.append(s)
        for s in bound:
            if ep.host and ep.host not in s.net_hosts:
                s.net_hosts.append(ep.host)
            if s.label not in ep.called_from:
                ep.called_from.append(s.label)


def _attribute_permissions(m: AppMap, idx: SourceIndex) -> None:
    present = {p.rsplit(".", 1)[-1] for p in m.dangerous_permissions}
    simple2screen = {scr.id.rsplit(".", 1)[-1]: scr for scr in m.screens}
    usage: dict[str, dict] = {}
    for perm in present:
        spec = _PERM_APIS.get(perm)
        if not spec:
            continue
        api_label, needles = spec
        where: list[str] = []
        api_seen = set()
        for f in idx.files:
            hit = next((n for n in needles if n in f.text), None)
            if not hit:
                continue
            api_seen.add(hit)
            simple = f.dotted.rsplit(".", 1)[-1].split("$", 1)[0]
            label = simple
            if simple in simple2screen:
                scr = simple2screen[simple]
                if perm not in scr.uses_perms:
                    scr.uses_perms.append(perm)
            if label not in where and f.first_party:
                where.append(label)
        if where or api_seen:
            usage[perm] = {
                "permission": perm, "api": api_label,
                "where": where[:8],
                "signals": sorted(api_seen)[:4],
            }
    m.perm_usage = list(usage.values())


def _journeys(m: AppMap) -> list[dict]:
    if not m.screens:
        return []
    id2s = {s.id: s for s in m.screens}
    adj = defaultdict(list)
    for e in m.edges:
        if e.src in id2s and e.dst in id2s:
            adj[e.src].append(e.dst)
    entries = [s.id for s in m.screens if s.is_entry] or \
              [s.id for s in m.screens if not any(e.dst == s.id for e in m.edges)][:1]
    # Targets worth telling a story about.
    want = ("payment", "auth", "chat", "media", "map")
    targets = [s for s in m.screens if s.purpose in want]
    journeys: list[dict] = []
    seen_targets = set()
    for tgt in sorted(targets, key=lambda s: want.index(s.purpose)):
        if tgt.id in seen_targets:
            continue
        path = _shortest_path(entries, tgt.id, adj)
        if not path or len(path) < 2:
            continue
        seen_targets.add(tgt.id)
        journeys.append({
            "purpose": tgt.purpose,
            "label": id2s[tgt.id].label,
            "path": [id2s[p].label for p in path],
            "path_purposes": [id2s[p].purpose for p in path],
        })
        if len(journeys) >= 8:
            break
    return journeys


def _shortest_path(sources: list[str], target: str, adj: dict) -> list[str]:
    prev = {}
    dq = deque()
    for s in sources:
        prev[s] = None
        dq.append(s)
    while dq:
        n = dq.popleft()
        if n == target:
            out = []
            while n is not None:
                out.append(n)
                n = prev[n]
            return list(reversed(out))
        for t in adj[n]:
            if t not in prev:
                prev[t] = n
                dq.append(t)
    return []


def _deep_link_catalog(m: AppMap) -> list[dict]:
    out: list[dict] = []
    pkg = m.package
    for c in m.components:
        if not c.deep_link_schemes:
            continue
        hosts = c.deep_link_hosts or [""]
        paths = c.deep_link_paths or [""]
        for scheme in c.deep_link_schemes:
            for host in hosts:
                for path in paths[:3]:
                    if scheme in ("http", "https") and not host:
                        continue
                    uri = f"{scheme}://{host}{path}" if host or path else f"{scheme}://"
                    adb = ('adb shell am start -a android.intent.action.VIEW '
                           f'-d "{uri}" {pkg}')
                    out.append({"uri": uri, "component": c.name.rsplit(".", 1)[-1],
                                "exported": c.exported, "adb": adb})
    # dedupe by uri
    seen, uniq = set(), []
    for d in out:
        if d["uri"] in seen:
            continue
        seen.add(d["uri"])
        uniq.append(d)
    return uniq[:40]


def _external_surface(m: AppMap) -> list[dict]:
    out = []
    for c in m.components:
        if not c.exported or c.is_launcher:
            continue
        if c.permission:                      # guarded — less interesting as free surface
            guard = "permission-guarded"
        else:
            guard = "no guard"
        acts = [a.rsplit(".", 1)[-1] for a in c.intent_actions
                if not a.startswith("android.intent.action.MAIN")]
        if not acts and not c.deep_link_schemes:
            continue
        out.append({
            "component": c.name.rsplit(".", 1)[-1], "kind": c.kind,
            "guard": guard, "actions": acts,
            "schemes": [s + "://" for s in c.deep_link_schemes],
        })
    return out[:30]


# ---------------------------------------------------------------------------
def _summary(m: AppMap) -> str:
    if not m.package:
        return ""
    # language / ui toolkit
    kotlin = any("Coroutines" in t or "kotlinx" in t for t in m.tech_stack)
    lang = "Kotlin" if kotlin else "Java/Kotlin"
    ui = ("Jetpack Compose" if "Jetpack Compose" in m.tech_stack
          else "the classic View/XML system" if "View system (XML)" in m.tech_stack
          else "an unidentified UI toolkit")
    di = next((t for t in m.tech_stack if "DI" in t), "")
    arch = "MVVM" if m.roles.get("ViewModels") else ("MVC/other")

    # what kind of app
    purposes = {s.purpose for s in m.screens}
    kind_bits = []
    if "payment" in purposes:
        kind_bits.append("in-app payments/subscriptions")
    if "auth" in purposes:
        kind_bits.append("user accounts")
    if "chat" in purposes:
        kind_bits.append("messaging")
    if "map" in purposes:
        kind_bits.append("location features")
    if "media" in purposes:
        kind_bits.append("camera/media")

    hosts = sorted({ep.host for ep in m.endpoints if ep.host})
    entry = next((s.label for s in m.screens if s.is_entry), "an unnamed launcher")

    parts = []
    if m.framework_note:
        parts.append(f"This is a **{m.framework}** app, so the Java/Kotlin view below is only the "
                     f"host shell — its real logic is compiled out of reach of this static map.")
    kw = f" It centres on {', '.join(kind_bits)}." if kind_bits else ""
    n_compose = len([s for s in m.screens if s.kind in ("compose-screen", "compose-route")])
    struct = (f"{m.roles.get('ViewModels',0)} ViewModels, "
              f"{len([s for s in m.screens if s.kind=='activity'])} activities, "
              f"{len([s for s in m.screens if s.kind=='fragment'])} fragments")
    if n_compose:
        struct += f", {n_compose} Compose screens"
    parts.append(
        f"**{m.package}** (v{m.version_name}) is a native {lang} Android app of roughly "
        f"{m.first_party_files} first-party classes, built with {ui}"
        + (f" and {di.replace(' (DI)','')} for dependency injection" if di else "")
        + f", following a broadly {arch} structure ({struct}).{kw}")

    if hosts:
        parts.append(
            f"It talks to {len(hosts)} backend host(s) — {', '.join(hosts[:5])}"
            + ("…" if len(hosts) > 5 else "")
            + f" — over {', '.join(m.network_libs[:3]) or 'HTTP'}, and persists data with "
            f"{', '.join(m.storage[:3]) or 'no obvious local store'}.")

    flow = next((j for j in m.journeys if j["purpose"] == "payment"), None) or \
        (m.journeys[0] if m.journeys else None)
    if flow:
        parts.append(f"A key user path runs {' ▸ '.join(flow['path'])}.")

    if m.dangerous_permissions:
        dp = ", ".join(p.rsplit(".", 1)[-1] for p in m.dangerous_permissions[:6])
        parts.append(f"It requests sensitive permissions ({dp}) and integrates "
                     f"{len(m.third_party_sdks)} third-party SDK(s).")
    return " ".join(parts)
