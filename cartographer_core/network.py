"""Map the app's network surface: endpoints, base URLs, and HTTP stacks.

Retrofit annotation strings, `baseUrl(...)`, URL literals and `loadUrl(...)` all
survive obfuscation (they're string constants), so this section stays meaningful
even on heavily minified apps.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from .model import AppMap, Endpoint
from .scan import SourceIndex, SourceFile

_HTTP_ANNOT = re.compile(
    r'@(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\(\s*"([^"]*)"')
_BASE_URL = re.compile(r'\.baseUrl\(\s*"([^"]+)"')
_URL_LITERAL = re.compile(r'"(https?://[^\s"\'<>]{4,200})"')
_LOAD_URL = re.compile(r'loadUrl\(\s*"([^"]+)"')

# host substrings we never care about (schemas, doc URLs baked into libs)
_NOISE_HOSTS = (
    "schemas.android.com", "w3.org", "apache.org", "example.com", "goo.gl/",
    "developer.android.com", "github.com/", "gnu.org", "json-schema.org",
    "localhost", "127.0.0.1", "google.com/policies",
)

_LIB_SIGNS = [
    ("Retrofit", ("retrofit2", "Retrofit.Builder")),
    ("OkHttp", ("okhttp3", "OkHttpClient")),
    ("Ktor client", ("io.ktor.client", "HttpClient(")),
    ("Volley", ("com.android.volley", "RequestQueue")),
    ("Apollo (GraphQL)", ("com.apollographql", "ApolloClient")),
    ("HttpURLConnection", ("HttpURLConnection",)),
    ("Fuel", ("com.github.kittinunf.fuel",)),
    ("WebView", ("WebView", ".loadUrl(")),
    ("Firebase / gRPC", ("firebaseio.com", "grpc")),
]


def build(m: AppMap, idx: SourceIndex, log=print) -> None:
    endpoints: list[Endpoint] = []
    base_urls: set[str] = set()
    libs: set[str] = set()
    seen_ep: set[tuple] = set()

    # Prefer first-party for endpoints, but scan everything for base URLs / libs.
    for f in idx.files:
        text = f.text
        for name, needles in _LIB_SIGNS:
            if name not in libs and any(n in text for n in needles):
                libs.add(name)

        for m2 in _BASE_URL.finditer(text):
            base_urls.add(m2.group(1))

        scan_endpoints = f.first_party or _looks_like_api(f)
        if not scan_endpoints:
            continue

        for mm in _HTTP_ANNOT.finditer(text):
            method, path = mm.group(1), mm.group(2)
            key = (method, path)
            if key in seen_ep:
                continue
            seen_ep.add(key)
            endpoints.append(Endpoint(method=method, path=path,
                                      source="retrofit", declared_in=_loc(f, mm.start(), text)))
        for mm in _LOAD_URL.finditer(text):
            url = mm.group(1)
            if _noise(url):
                continue
            key = ("WEBVIEW", url)
            if key in seen_ep:
                continue
            seen_ep.add(key)
            endpoints.append(Endpoint(method="WEBVIEW", path=url, host=_host(url),
                                      source="webview", declared_in=_loc(f, mm.start(), text)))
        for mm in _URL_LITERAL.finditer(text):
            url = mm.group(1)
            if _noise(url):
                continue
            key = ("URL", url)
            if key in seen_ep:
                continue
            seen_ep.add(key)
            endpoints.append(Endpoint(method="URL", path=url, host=_host(url),
                                      source="literal", declared_in=_loc(f, mm.start(), text)))

    # Resolve retrofit relative paths against a single base url, and fill hosts.
    single_base = next(iter(base_urls)) if len(base_urls) == 1 else ""
    for ep in endpoints:
        if not ep.host:
            if ep.path.startswith("http"):
                ep.host = _host(ep.path)
            elif single_base:
                ep.host = _host(single_base)

    m.endpoints = sorted(endpoints, key=lambda e: (e.host, e.method, e.path))
    m.base_urls = sorted(base_urls)
    m.network_libs = sorted(libs)
    log(f"[*] Found {len(m.endpoints)} endpoint(s) across {len({e.host for e in endpoints if e.host}) or 0} host(s); "
        f"stacks: {', '.join(m.network_libs) or 'none detected'}.")


def _looks_like_api(f: SourceFile) -> bool:
    n = f.dotted.rsplit(".", 1)[-1].lower()
    return any(k in n for k in ("api", "service", "client", "endpoint", "retrofit", "network"))


def _noise(url: str) -> bool:
    return any(n in url for n in _NOISE_HOSTS)


def _host(url: str) -> str:
    try:
        h = urlparse(url).netloc
        return h or ""
    except Exception:  # noqa: BLE001
        return ""


def _loc(f: SourceFile, idx: int, text: str) -> str:
    line = text.count("\n", 0, idx) + 1
    return f"{f.rel}:{line}"
