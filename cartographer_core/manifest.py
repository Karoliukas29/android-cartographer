"""Manifest inventory: identity, permissions, and every declared component.

The manifest is the most obfuscation-resistant surface in an APK — component
class names must stay real for the OS to launch them, so this is the backbone of
the screen map.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from typing import Optional

from .decompile import sha256_file
from .model import AppMap, Component

ANDROID_NS = "http://schemas.android.com/apk/res/android"
_A = f"{{{ANDROID_NS}}}"

# A pragmatic subset of the runtime-"dangerous" permission groups.
_DANGEROUS = {
    "READ_CONTACTS", "WRITE_CONTACTS", "GET_ACCOUNTS",
    "ACCESS_FINE_LOCATION", "ACCESS_COARSE_LOCATION", "ACCESS_BACKGROUND_LOCATION",
    "READ_CALENDAR", "WRITE_CALENDAR", "CAMERA", "RECORD_AUDIO",
    "READ_PHONE_STATE", "READ_PHONE_NUMBERS", "CALL_PHONE", "READ_CALL_LOG",
    "WRITE_CALL_LOG", "ADD_VOICEMAIL", "USE_SIP", "ANSWER_PHONE_CALLS",
    "BODY_SENSORS", "SEND_SMS", "RECEIVE_SMS", "READ_SMS", "RECEIVE_WAP_PUSH",
    "RECEIVE_MMS", "READ_EXTERNAL_STORAGE", "WRITE_EXTERNAL_STORAGE",
    "READ_MEDIA_IMAGES", "READ_MEDIA_VIDEO", "READ_MEDIA_AUDIO",
    "ACCESS_MEDIA_LOCATION", "ACTIVITY_RECOGNITION", "POST_NOTIFICATIONS",
}

# Human-readable device capability per (dangerous) permission keyword.
_CAP_HINTS = [
    ("LOCATION", "Location tracking"),
    ("CAMERA", "Camera capture"),
    ("RECORD_AUDIO", "Microphone / audio recording"),
    ("CONTACTS", "Reads the contact book"),
    ("SMS", "Reads / sends SMS"),
    ("CALL", "Phone-call access"),
    ("PHONE_STATE", "Device / SIM identity"),
    ("STORAGE", "External storage access"),
    ("MEDIA", "Photo / video / audio library"),
    ("BODY_SENSORS", "Body sensors (heart rate, etc.)"),
    ("ACTIVITY_RECOGNITION", "Physical-activity recognition"),
    ("BLUETOOTH", "Bluetooth"),
    ("NFC", "NFC"),
    ("ACCOUNTS", "Device accounts"),
]


def load(apk_path: str, log=print):
    """Return (AppMap seeded with identity/permissions/components, androguard apk, manifest root)."""
    import logging
    logging.getLogger("androguard").setLevel(logging.CRITICAL)
    from androguard.core.bytecodes.apk import APK

    apk = APK(apk_path)
    m = AppMap(path=apk_path)
    m.sha256 = sha256_file(apk_path)
    m.file_size = os.path.getsize(apk_path)
    m.package = apk.get_package() or ""
    m.version_name = apk.get_androidversion_name() or ""
    m.version_code = str(apk.get_androidversion_code() or "")
    try:
        m.min_sdk = str(apk.get_min_sdk_version() or "")
        m.target_sdk = str(apk.get_target_sdk_version() or "")
    except Exception:  # noqa: BLE001
        pass

    perms = sorted(apk.get_permissions() or [])
    m.permissions = perms
    m.dangerous_permissions = [p for p in perms if p.rsplit(".", 1)[-1] in _DANGEROUS]
    m.capabilities = _capabilities(perms)

    root = None
    try:
        root = ET.fromstring(apk.get_android_manifest_axml().get_xml())
    except Exception:  # noqa: BLE001
        root = None
    if root is not None:
        m.components = _components(root, m.package)

    try:
        libs = {os.path.basename(f) for f in apk.get_files()
                if f.startswith("lib/") and f.endswith(".so")}
        m.native_libs = sorted(libs)
    except Exception:  # noqa: BLE001
        pass
    return m, apk, root


def _capabilities(perms: list[str]) -> list[str]:
    out: list[str] = []
    joined = " ".join(perms).upper()
    for needle, label in _CAP_HINTS:
        if needle in joined and label not in out:
            out.append(label)
    return out


def _name(el: ET.Element, pkg: str) -> str:
    name = el.get(f"{_A}name", "")
    if name.startswith(".") and pkg:
        name = pkg + name
    elif name and "." not in name and pkg:
        name = f"{pkg}.{name}"
    return name


def _is_launcher(el: ET.Element) -> bool:
    for intent in el.findall("intent-filter"):
        acts = {a.get(f"{_A}name", "") for a in intent.findall("action")}
        cats = {c.get(f"{_A}name", "") for c in intent.findall("category")}
        if "android.intent.action.MAIN" in acts and "android.intent.category.LAUNCHER" in cats:
            return True
    return False


def _is_exported(el: ET.Element) -> bool:
    exp = el.get(f"{_A}exported")
    if exp is not None:
        return exp.lower() == "true"
    return el.find("intent-filter") is not None


def _components(root: ET.Element, pkg: str) -> list[Component]:
    out: list[Component] = []
    for tag, kind in (("activity", "Activity"), ("activity-alias", "Activity"),
                      ("service", "Service"), ("receiver", "Receiver"),
                      ("provider", "Provider")):
        for el in root.findall(f".//{tag}"):
            name = _name(el, pkg)
            if not name:
                continue
            actions, schemes, hosts, paths = [], [], [], []
            for intent in el.findall("intent-filter"):
                for a in intent.findall("action"):
                    an = a.get(f"{_A}name", "")
                    if an:
                        actions.append(an)
                for d in intent.findall("data"):
                    s = d.get(f"{_A}scheme", "")
                    h = d.get(f"{_A}host", "")
                    if s:
                        schemes.append(s)
                    if h:
                        hosts.append(h)
                    for attr in ("path", "pathPrefix", "pathPattern"):
                        p = d.get(f"{_A}{attr}", "")
                        if p:
                            paths.append(p)
            out.append(Component(
                name=name, kind=kind, exported=_is_exported(el),
                is_launcher=_is_launcher(el),
                permission=el.get(f"{_A}permission"),
                intent_actions=sorted(set(actions)),
                deep_link_schemes=sorted(set(schemes)),
                deep_link_hosts=sorted(set(hosts)),
                deep_link_paths=sorted(set(paths)),
            ))
    return out
