#!/usr/bin/env python3
"""Deterministic test for the Compose call-graph pass.

Mimics how jadx decompiles Jetpack Compose Navigation: screen bodies call child
screens directly, and callback navigation lives in synthetic `$Screen$` classes.
Run: python3 tests/test_callgraph.py   (exit 0 = pass)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cartographer_core import callgraph
from cartographer_core.model import AppMap, Screen
from cartographer_core.scan import SourceIndex, SourceFile


def sf(dotted, text):
    return SourceFile(path=dotted, rel=dotted.replace(".", "/") + ".java",
                      dotted=dotted, text=text, first_party=True)


def compose(dotted, fn):
    return Screen(id=f"{dotted}#{fn}", label=fn, kind="compose-screen")


def run():
    m = AppMap(package="com.x")
    m.screens = [
        compose("com.x.HomeScreenKt", "HomeScreen"),
        compose("com.x.DetailScreenKt", "DetailScreen"),
        compose("com.x.SettingsScreenKt", "SettingsScreen"),
        Screen(id="com.x.LoginActivity", label="LoginActivity", kind="activity"),
    ]
    m.edges = []

    files = [
        # HomeScreen definition directly composes DetailScreen -> edge Home->Detail
        sf("com.x.HomeScreenKt",
           "public static final void HomeScreen(Composer composer, int i) {\n"
           "    DetailScreen(composer, 0);\n"
           "}\n"
           "public static final void DetailScreen(Composer composer, int i) {}\n"),
        # NavHost wires routes -> screens (for route resolution)
        sf("com.x.AppNavHostKt",
           'NavGraphBuilderKt.composable(b, "settings", null, null, X); SettingsScreen(c, 0);\n'),
        # synthetic callback lambda of HomeScreen navigates by route -> Home->Settings
        sf("com.x.HomeScreenKt$HomeScreen$1",
           'navController.navigate("settings");\n'),
        # another HomeScreen lambda launches an Activity -> Home->LoginActivity
        sf("com.x.HomeScreenKt$HomeScreen$2",
           "startActivity(new android.content.Intent(this, (java.lang.Class<?>) com.x.LoginActivity.class));\n"),
        # a non-screen composable call must NOT create an edge
        sf("com.x.DetailScreenKt$DetailScreen$1", "MyCustomHeader(composer, 0);\n"),
    ]
    idx = SourceIndex(outdir="/tmp", files=files)

    callgraph.build(m, idx, log=lambda *a: None)
    got = {(e.src.split("#")[-1].split(".")[-1], e.dst.split("#")[-1].split(".")[-1], e.via)
           for e in m.edges}
    expected = {
        ("HomeScreen", "DetailScreen", "compose"),
        ("HomeScreen", "SettingsScreen", "compose"),
        ("HomeScreen", "LoginActivity", "intent"),
    }
    ok = got == expected
    print("got     :", sorted(got))
    print("expected:", sorted(expected))
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(run())
