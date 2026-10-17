"""Runtime pieces that outlived the old cloud-VM plan (now in legacy/, not used), offline: Chrome's
per-platform launch, the optional noVNC link in ready_for_you pings and on the dashboard, /healthz and
the SQLite backup script."""
from __future__ import annotations

import gzip
import importlib.util
import json
import sqlite3
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from goat import config, dashboard, db, worker
from goat.browser import chrome

ROOT = Path(__file__).resolve().parent.parent
NOVNC = "http://goat-vm:6080/vnc.html?autoconnect=1&resize=scale"


# ------------------------------------------------------------------ Chrome on Linux
def test_default_chrome_exe_by_platform():
    assert config.default_chrome_exe("win32") == r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    assert config.default_chrome_exe("darwin").startswith("/Applications/Google Chrome.app/")
    assert config.default_chrome_exe("linux", exists=lambda p: True) == "/usr/bin/google-chrome"
    assert config.default_chrome_exe("linux", exists=lambda p: p == "/usr/bin/chromium") == "/usr/bin/chromium"
    assert config.default_chrome_exe("linux", exists=lambda p: False) == "/usr/bin/google-chrome"


def _browser(**kw) -> config.Browser:
    b = config.Browser()
    for k, v in kw.items():
        setattr(b, k, v)
    return b


def test_linux_launch_draws_on_xvfb_with_the_dedicated_profile():
    b = _browser(chrome_exe="/usr/bin/google-chrome", user_data_dir="/app/chrome-profile", cdp_port=9223,
                 display=":99", no_sandbox=True)
    args, env = chrome.launch_command(b, platform="linux")
    assert args[0] == "/usr/bin/google-chrome" and args[-1] == "about:blank"
    assert "--user-data-dir=/app/chrome-profile" in args
    assert "--remote-debugging-port=9223" in args and "--remote-debugging-address=127.0.0.1" in args
    for flag in ("--window-size=1440,900", "--window-position=0,0", "--lang=en-US", "--no-sandbox"):
        assert flag in args
    assert "--window-size=1456,1000" not in args
    assert env["DISPLAY"] == ":99" and env["LANGUAGE"].startswith("en_US")


def test_windows_launch_is_unchanged():
    b = _browser(chrome_exe=r"C:\chrome.exe", user_data_dir=r"C:\profile", no_sandbox=True)
    args, env = chrome.launch_command(b, platform="win32")
    assert env is None
    assert "--window-size=1456,1000" in args and "--user-data-dir=C:\\profile" in args
    assert not any(f in args for f in ("--lang=en-US", "--no-sandbox", "--window-size=1440,900"))


# ------------------------------------------------------------------ noVNC link
def test_ready_for_you_ping_carries_the_novnc_link():
    t = worker.ready_for_you_text("Acme", "SWE Intern", "ready_for_you: hCaptcha then Submit",
                                  "https://jobs.lever.co/acme/1/apply", NOVNC)
    assert t.splitlines() == ["One click from done: Acme — SWE Intern", "ready_for_you: hCaptcha then Submit",
                              f"Finish it in the cloud browser: {NOVNC}", "Tab: https://jobs.lever.co/acme/1/apply"]
    # a long reason and tab URL are cut, never the link (Telegram caps a photo caption at 1024)
    long = worker.ready_for_you_text("Acme", "SWE Intern", "x" * 5000, "https://x.com/" + "a" * 5000, NOVNC)
    assert len(long) <= worker.READY_TEXT_MAX and NOVNC in long
    # on the PC there is no cloud browser to point at
    assert "cloud browser" not in worker.ready_for_you_text("Acme", "SWE", "r", "https://t", "")


@pytest.fixture()
def ready_job(tmp_path, monkeypatch):
    db.reset_for_tests(tmp_path / "t.db")
    with db.tx() as con:
        con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES('L1','Acme','SWE Intern','https://jobs.lever.co/acme/1','lever')")
        con.execute("INSERT INTO jobs(listing_id,status,lane,created_at,updated_at,hold_reason) "
                    "VALUES('L1','ready_for_you','ready_for_you',1,1,'ready_for_you: hCaptcha then Submit')")
    db.event(1, "job:ready_for_you", {"reason": "ready_for_you: hCaptcha then Submit"})
    monkeypatch.setattr(dashboard.SETTINGS.human, "novnc_url", NOVNC)
    return 1


def test_dashboard_offers_the_cloud_browser_for_ready_jobs(ready_job):
    n = dashboard.api_now()
    assert n["novnc_url"] == NOVNC and n["counts"]["ready_for_you"] == 1
    assert dashboard.api_job(ready_job)["novnc_url"] == NOVNC
    assert "Open cloud browser" in dashboard.HTML and "d.novnc_url" in dashboard.HTML and "n.novnc_url" in dashboard.HTML


# ------------------------------------------------------------------ /healthz
def test_healthz(ready_job, monkeypatch):
    monkeypatch.setattr(dashboard, "_cdp_ok", lambda: True)
    code, body = dashboard.api_healthz()
    assert code == 200 and body["ok"] and body["cdp"] and body["db"] and body["last_event_age_s"] is not None
    monkeypatch.setattr(dashboard, "_cdp_ok", lambda: False)
    code, body = dashboard.api_healthz()
    assert code == 503 and not body["ok"] and not body["cdp"]
    # a quiet run fails only when HEALTH_MAX_EVENT_AGE_S asks for it
    monkeypatch.setattr(dashboard, "_cdp_ok", lambda: True)
    with db.tx() as con:
        con.execute("UPDATE events SET ts=ts-7200")
    assert dashboard.api_healthz()[0] == 200
    monkeypatch.setattr(dashboard.SETTINGS, "health_max_event_age_s", 3600)
    code, body = dashboard.api_healthz()
    assert code == 503 and body["stale"]


def test_cdp_check_is_false_when_nothing_listens(monkeypatch):
    monkeypatch.setattr(dashboard.SETTINGS.browser, "cdp_port", 9)
    assert dashboard._cdp_ok() is False


def test_healthz_over_http(ready_job, monkeypatch):
    monkeypatch.setattr(dashboard, "_cdp_ok", lambda: True)
    srv = dashboard.serve(0, "127.0.0.1")
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz")
        assert r.status == 200 and json.loads(r.read())["ok"] is True
        monkeypatch.setattr(dashboard, "_cdp_ok", lambda: False)
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz")
        assert ei.value.code == 503
    finally:
        srv.shutdown()
        srv.server_close()


# ------------------------------------------------------------------ nightly backup
def _backup_module():
    spec = importlib.util.spec_from_file_location("backup_db", ROOT / "scripts" / "backup_db.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_backup_copies_a_live_wal_database_and_prunes(tmp_path):
    bk = _backup_module()
    src = tmp_path / "goat.db"
    writer = sqlite3.connect(src)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE t(x)")
        writer.execute("INSERT INTO t VALUES (1),(2)")
        writer.commit()          # rows still in the WAL, writer still open: an online backup
        out = bk.backup(src, tmp_path / "backups", keep=3, now=0)
        assert out.name == "goat-19700101-000000.db.gz"
        restored = tmp_path / "restored.db"
        restored.write_bytes(gzip.decompress(out.read_bytes()))
        con = sqlite3.connect(restored)
        assert con.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2
        con.close()
        for day in range(1, 6):
            bk.backup(src, tmp_path / "backups", keep=3, now=day * 86400)
    finally:
        writer.close()
    assert sorted(p.name for p in (tmp_path / "backups").iterdir()) == [
        "goat-19700104-000000.db.gz", "goat-19700105-000000.db.gz", "goat-19700106-000000.db.gz"]


def test_backup_main_exit_codes_and_upload(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_UPLOAD_CMD", "")      # whatever .env says, these runs upload only when told to
    bk = _backup_module()
    src = tmp_path / "goat.db"
    con = sqlite3.connect(src)
    con.execute("CREATE TABLE t(x)")
    con.commit()
    con.close()
    f = Path("/b/goat-x.db.gz")
    assert bk.upload_argv("rclone copy {file} remote:goat-backups", f) == ["rclone", "copy", str(f), "remote:goat-backups"]
    assert bk.upload_argv("aws s3 cp --quiet", f)[-1] == str(f)
    py = Path(sys.executable).as_posix()
    assert bk.main(["--db", str(tmp_path / "missing.db"), "--dest", str(tmp_path / "b1")]) == 1
    ok_cmd = f'"{py}" -c "import os, sys; sys.exit(0 if os.path.isfile(sys.argv[1]) else 4)"'
    assert bk.main(["--db", str(src), "--dest", str(tmp_path / "b2"), "--upload-cmd", ok_cmd]) == 0
    assert bk.main(["--db", str(src), "--dest", str(tmp_path / "b3"), "--upload-cmd", f'"{py}" -c "raise SystemExit(3)"']) == 2
    assert len(list((tmp_path / "b3").glob("goat-*.db.gz"))) == 1     # a failed upload keeps the local copy
