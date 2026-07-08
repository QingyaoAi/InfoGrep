"""Scheduler tests: exercise plist generation and listing without touching launchd.

``launchctl`` is monkeypatched out so these run in CI; the real load/unload is a thin
subprocess call covered by the manual install path.
"""

import plistlib
import sys

import infogrep.scheduler as sched


def _no_launchctl(monkeypatch, agents_dir):
    monkeypatch.setattr(sched, "LAUNCH_AGENTS", agents_dir)
    monkeypatch.setattr(sched.subprocess, "run", lambda *a, **k: None)
    # launchd is macOS-only; force the platform check so these pass on any host OS.
    monkeypatch.setattr(sched.sys, "platform", "darwin")


def test_install_writes_valid_plist(tmp_path, monkeypatch):
    agents = tmp_path / "LaunchAgents"
    target = tmp_path / "proj"
    target.mkdir()
    _no_launchctl(monkeypatch, agents)

    path = sched.install(target, hour=4, minute=30)
    assert path.exists()
    data = plistlib.loads(path.read_bytes())

    assert data["Label"].startswith("com.infogrep.reindex.")
    assert data["StartCalendarInterval"] == {"Hour": 4, "Minute": 30}
    # Runs the current interpreter against the resolved target dir.
    assert data["ProgramArguments"][0] == sys.executable
    assert data["ProgramArguments"][-2:] == ["index", str(target.resolve())]
    assert "PATH" in data["EnvironmentVariables"]
    assert data["StandardOutPath"].endswith("reindex.log")
    # Log lives in the separate index location, NOT inside the indexed folder.
    assert str(target) not in data["StandardOutPath"]


def test_list_and_uninstall(tmp_path, monkeypatch):
    agents = tmp_path / "LaunchAgents"
    target = tmp_path / "proj"
    target.mkdir()
    _no_launchctl(monkeypatch, agents)

    sched.install(target, hour=2, minute=0)
    listed = sched.list_agents()
    assert len(listed) == 1
    assert listed[0]["directory"] == str(target.resolve())
    assert (listed[0]["hour"], listed[0]["minute"]) == (2, 0)

    assert sched.uninstall(target) is True
    assert sched.list_agents() == []
    assert sched.uninstall(target) is False  # already gone


def test_install_replaces_existing(tmp_path, monkeypatch):
    agents = tmp_path / "LaunchAgents"
    target = tmp_path / "proj"
    target.mkdir()
    _no_launchctl(monkeypatch, agents)

    sched.install(target, hour=1, minute=0)
    sched.install(target, hour=5, minute=15)  # same dir -> same label, replaced
    listed = sched.list_agents()
    assert len(listed) == 1
    assert (listed[0]["hour"], listed[0]["minute"]) == (5, 15)


def test_install_rejects_non_macos(tmp_path, monkeypatch):
    monkeypatch.setattr(sched, "LAUNCH_AGENTS", tmp_path / "LaunchAgents")
    monkeypatch.setattr(sched.sys, "platform", "linux")

    try:
        sched.install(tmp_path / "proj", hour=3, minute=0)
        assert False, "expected RuntimeError on non-macOS"
    except RuntimeError as exc:
        assert "cron" in str(exc) or "systemd" in str(exc)


def test_is_scheduled_roundtrip(tmp_path, monkeypatch):
    _no_launchctl(monkeypatch, tmp_path / "LaunchAgents")
    target = tmp_path / "proj"
    assert sched.is_scheduled(target) is False
    sched.install(target, hour=3, minute=0)
    assert sched.is_scheduled(target) is True
    sched.uninstall(target)
    assert sched.is_scheduled(target) is False
