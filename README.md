# Android Cartographer

Drop in an APK, get back a map of how the app actually works.

I kept starting engagements the same way: open the APK in jadx, click through
activities for an hour trying to figure out where the login is, what talks to the
network, which screen handles payments, what permissions get used where. This just
does that pass for me and hands back one HTML page I can read in two minutes.

It's the "understand it first" tool. It doesn't look for bugs — it reverse-maps the
app: the screen flow, the buttons on each screen and where they go, the API calls,
the tech stack, the permissions, the deep links. Then I know where to dig.

```bash
python3 cartographer.py app.apk
```

Open the `report.html` it writes and you get:

- a **plain-English summary** of what the app is and how it's built
- an **interactive screen-flow graph** — every screen and how you move between them
  (click a node to jump to it, search to filter)
- **user journeys** like `Splash → Login → Dashboard → Premium → Payment`
- each screen's **buttons/inputs**, the **backend hosts** it calls, and the
  **permissions** it actually uses
- the **network surface** — endpoints grouped by host
- a **deep-link list** with a ready-to-copy `adb` command to open each one

Works on obfuscated apps too — it leans on the stuff R8 can't rename (manifest
names, layouts, nav graphs, URL strings), and it tells you when an app is
Flutter/React-Native so you know the Java side is just the shell.

## Requirements

- **Python 3.10+**
- **`androguard` + `rich`** — `pip install -r requirements.txt`
- **`jadx`** — the decompiler (needs a **Java** runtime, JRE 11+). Put it on your
  `PATH` (or at `../tools/jadx`). Without it you still get a manifest-only map.

No Android SDK, emulator or adb needed — it's purely static.

## Running it

```bash
pip install -r requirements.txt
python3 cartographer.py app.apk        # or .xapk / .apkm / .apks
python3 cartographer.py app.apk -o out/
```

It's fully offline — no network, no API keys, no telemetry. Output goes to
`carto-out/<package>/` and is gitignored (it holds decompiled code and app
strings pulled from the target, so it stays on your machine).

Only run it on apps you own or are allowed to test.
