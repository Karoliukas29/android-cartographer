"""Architecture map: package tree, class roles, tech stack, storage, SDKs.

With jadx run `--no-imports`, third-party types appear fully-qualified inline in
the app's own decompiled code, so we can fingerprint the stack by scanning the
app's files (plus the manifest's real component names) without needing to read
the library packages themselves.
"""

from __future__ import annotations

import re
from collections import Counter

from .model import AppMap
from .scan import SourceIndex

# role -> (regex over class body)
_ROLE_SIGNS = [
    ("Activities", re.compile(r"(?:extends|:)\s*[\w.]*Activity\b")),
    ("Fragments", re.compile(r"(?:extends|:)\s*[\w.]*Fragment\b")),
    ("ViewModels", re.compile(r"(?:extends|:)\s*[\w.]*ViewModel\b")),
    ("Services", re.compile(r"(?:extends|:)\s*[\w.]*Service\b")),
    ("BroadcastReceivers", re.compile(r"(?:extends|:)\s*[\w.]*BroadcastReceiver\b")),
    ("RecyclerView adapters", re.compile(r"(?:extends|:)\s*[\w.]*Adapter\b")),
    ("@Composable functions", re.compile(r"@Composable")),
    ("Room entities", re.compile(r"@Entity\b")),
    ("Room DAOs", re.compile(r"@Dao\b")),
    ("Retrofit interfaces", re.compile(r"@(?:GET|POST|PUT|DELETE|PATCH)\(")),
    ("Repositories", re.compile(r"class\s+\w*Repository|Repository\s*[({]|Repository\b")),
    ("Use cases", re.compile(r"\w+UseCase\b")),
    ("Workers", re.compile(r"(?:extends|:)\s*[\w.]*Worker\b")),
]

_STACK_SIGNS = [
    ("Jetpack Compose", ("androidx.compose", "@Composable", "setContent {")),
    ("View system (XML)", ("setContentView(", "R.layout.")),
    ("Hilt (DI)", ("dagger.hilt", "@AndroidEntryPoint", "@HiltAndroidApp")),
    ("Dagger (DI)", ("dagger.Component", "dagger.Module")),
    ("Koin (DI)", ("org.koin",)),
    ("Room (DB)", ("androidx.room", "@Entity", "@Dao")),
    ("SQLite (raw)", ("SQLiteOpenHelper", "rawQuery(")),
    ("DataStore", ("androidx.datastore", "preferencesDataStore")),
    ("Realm (DB)", ("io.realm",)),
    ("RxJava", ("io.reactivex",)),
    ("Coroutines / Flow", ("kotlinx.coroutines", "suspend fun", "StateFlow")),
    ("Navigation component", ("androidx.navigation", "NavController", "findNavController")),
    ("WorkManager", ("androidx.work", "WorkManager")),
    ("Retrofit", ("retrofit2",)),
    ("OkHttp", ("okhttp3",)),
    ("Glide", ("com.bumptech.glide",)),
    ("Coil", ("coil.load", "io.coil-kt", "coil.compose")),
    ("Picasso", ("com.squareup.picasso",)),
    ("Gson", ("com.google.gson",)),
    ("Moshi", ("com.squareup.moshi",)),
    ("kotlinx.serialization", ("kotlinx.serialization", "@Serializable")),
    ("ExoPlayer / Media3", ("com.google.android.exoplayer", "androidx.media3")),
    ("Lottie", ("com.airbnb.lottie",)),
    ("Paging", ("androidx.paging",)),
    ("Google Maps", ("com.google.android.gms.maps",)),
    ("CameraX", ("androidx.camera",)),
    ("ML Kit", ("com.google.mlkit",)),
]

# SDK signatures matched against BOTH app code and manifest component names.
_SDK_SIGNS = [
    ("Firebase Analytics", ("firebase.analytics", "FirebaseAnalytics", "google_app_measurement")),
    ("Firebase Crashlytics", ("firebase.crashlytics", "CrashlyticsInitProvider")),
    ("Firebase Cloud Messaging", ("firebase.messaging", "FirebaseMessagingService")),
    ("Firebase Auth", ("firebase.auth",)),
    ("Firebase Firestore", ("firebase.firestore",)),
    ("Google AdMob / Ads", ("android.gms.ads", "AdActivity")),
    ("Google Play Services", ("android.gms.common",)),
    ("Facebook SDK", ("com.facebook.", "FacebookActivity")),
    ("AppsFlyer", ("com.appsflyer",)),
    ("Adjust", ("com.adjust.sdk",)),
    ("Amplitude", ("com.amplitude",)),
    ("Mixpanel", ("com.mixpanel",)),
    ("Segment", ("com.segment.analytics",)),
    ("Braze", ("com.braze", "com.appboy")),
    ("OneSignal", ("com.onesignal",)),
    ("Sentry", ("io.sentry",)),
    ("Bugsnag", ("com.bugsnag",)),
    ("Branch", ("io.branch",)),
    ("Stripe", ("com.stripe.android", "com.stripe",)),
    ("Braintree / PayPal", ("com.braintreepayments", "com.paypal")),
    ("Intercom", ("io.intercom",)),
    ("Zendesk", ("com.zendesk", "zendesk.")),
]

_STORAGE_SIGNS = [
    ("SharedPreferences", ("getSharedPreferences", "PreferenceManager", "getDefaultSharedPreferences")),
    ("EncryptedSharedPreferences", ("EncryptedSharedPreferences",)),
    ("Room / SQLite database", ("androidx.room", "SQLiteOpenHelper", "SQLiteDatabase")),
    ("DataStore", ("androidx.datastore", "preferencesDataStore")),
    ("Internal files", ("openFileOutput(", "getFilesDir(", "FileOutputStream(")),
    ("External storage", ("getExternalFilesDir", "Environment.getExternalStorage")),
    ("Android Keystore", ("AndroidKeyStore", 'KeyStore.getInstance("AndroidKeyStore")')),
    ("Realm", ("io.realm",)),
    ("MMKV", ("com.tencent.mmkv", "MMKV.")),
]


def build(m: AppMap, idx: SourceIndex, log=print) -> None:
    m.total_files = idx.total_files
    m.first_party_files = len(idx.first_party)
    m.library_files = idx.library_files

    scan_files = idx.first_party or idx.files
    corpus_files = idx.files  # app code with inline FQNs (jadx --no-imports)

    # Roles (per class body). Skip inner-class/lambda files (Foo$Bar) so a single
    # screen's anonymous classes don't each inflate the count.
    roles: Counter = Counter()
    for f in scan_files:
        if "$" in f.dotted.rsplit(".", 1)[-1]:
            continue
        for role, rx in _ROLE_SIGNS:
            if rx.search(f.text):
                roles[role] += 1
    m.roles = {k: v for k, v in roles.most_common() if v}

    # Package tree — first-party packages by file count.
    pkg_counts: Counter = Counter()
    for f in (idx.first_party or idx.files):
        pkg = f.dotted.rsplit(".", 1)[0]
        pkg_counts[pkg] += 1
    m.package_tree = [{"package": p, "files": c} for p, c in pkg_counts.most_common(25)]

    # Tech stack + storage: scan app corpus text.
    joined = "\n".join(f.text for f in corpus_files)
    comp_names = "\n".join(c.name for c in m.components)
    m.tech_stack = [name for name, needles in _STACK_SIGNS
                    if any(n in joined for n in needles)]
    m.storage = [name for name, needles in _STORAGE_SIGNS
                 if any(n in joined for n in needles)]

    haystack = joined + "\n" + comp_names
    m.third_party_sdks = [name for name, needles in _SDK_SIGNS
                          if any(n in haystack for n in needles)]

    log(f"[*] Stack: {', '.join(m.tech_stack) or 'unknown'}; "
        f"{len(m.third_party_sdks)} third-party SDK(s); "
        f"storage: {', '.join(m.storage) or 'none detected'}.")
