"""The chat PWA's shell: every file the service worker pre-caches and every icon the manifest
names must actually be served. cache.addAll() is all-or-nothing, so a single 404 in that
list means the worker never installs, and a missing manifest icon breaks Android installs."""
from __future__ import annotations

import json
import re

import pytest

import config


@pytest.fixture
def client(db):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    import hestia
    return fastapi_testclient.TestClient(hestia.app)


def _sw_shell() -> list[str]:
    src = (config.CLIENTS_DIR / "sw.js").read_text()
    paths = []
    for name in ("PAGES", "STATIC"):
        m = re.search(rf"const {name} = \[(.*?)\];", src)
        assert m, f"sw.js no longer declares {name}"
        paths += re.findall(r"'([^']+)'", m.group(1))
    return paths


def test_every_precached_path_is_served(client):
    paths = _sw_shell()
    assert "/app" in paths
    for p in paths:
        assert client.get(p).status_code == 200, p


def test_every_manifest_icon_is_served(client):
    manifest = json.loads((config.CLIENTS_DIR / "manifest.json").read_text())
    for icon in manifest["icons"]:
        r = client.get(icon["src"])
        assert r.status_code == 200, icon["src"]
        assert r.headers["content-type"] == "image/png"


def test_sized_icons_are_distinct_files(client):
    assert client.get("/icon-192.png").content == (config.CLIENTS_DIR / "icon-192.png").read_bytes()
    assert client.get("/icon-512.png").content == (config.CLIENTS_DIR / "icon-512.png").read_bytes()


def test_app_assets_serve_only_flat_css_and_js(client, tmp_path, monkeypatch):
    (tmp_path / "chat.css").write_text("body{}")
    (tmp_path / "chat.js").write_text("1")
    (tmp_path / "secret.txt").write_text("no")
    monkeypatch.setattr(config, "CLIENTS_DIR", tmp_path)
    css, js = client.get("/app/chat.css"), client.get("/app/chat.js")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    assert js.status_code == 200 and js.headers["content-type"].startswith("application/javascript")
    for bad in ("/app/secret.txt", "/app/Chat.css", "/app/..%2Fsw.js", "/app/missing.js"):
        assert client.get(bad).status_code == 404, bad


def test_clips_page_redirects_into_the_app(client):
    r = client.get("/clips", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/app#library"
