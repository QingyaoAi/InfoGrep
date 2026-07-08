"""JDK discovery: exercise the candidate list on both macOS and Linux layouts."""

import infogrep.jvm as jvm


def _no_subprocess(monkeypatch):
    monkeypatch.setattr(jvm.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(jvm.shutil, "which", lambda *_a, **_k: None)
    monkeypatch.setattr(jvm, "LINUX_JVM_DIR", jvm.Path("/nonexistent-jvm-dir"))


def test_candidates_include_java_home_env(monkeypatch):
    monkeypatch.setenv("JAVA_HOME", "/opt/some-jdk")
    _no_subprocess(monkeypatch)
    candidates = jvm._candidates()
    assert candidates[0] == jvm.Path("/opt/some-jdk")


def test_candidates_include_linux_usr_lib_jvm(tmp_path, monkeypatch):
    jvm_dir = tmp_path / "jvm"
    (jvm_dir / "java-21-openjdk-amd64").mkdir(parents=True)
    (jvm_dir / "java-11-openjdk-amd64").mkdir(parents=True)
    monkeypatch.delenv("JAVA_HOME", raising=False)
    _no_subprocess(monkeypatch)
    monkeypatch.setattr(jvm, "LINUX_JVM_DIR", jvm_dir)

    candidates = [str(p) for p in jvm._candidates()]
    assert any("java-21-openjdk-amd64" in c for c in candidates)
    assert any("java-11-openjdk-amd64" in c for c in candidates)


def test_candidates_include_which_java(monkeypatch, tmp_path):
    fake_home = tmp_path / "jdk-21"
    fake_bin = fake_home / "bin"
    fake_bin.mkdir(parents=True)
    fake_java = fake_bin / "java"
    fake_java.write_text("")

    monkeypatch.delenv("JAVA_HOME", raising=False)
    _no_subprocess(monkeypatch)
    monkeypatch.setattr(jvm.shutil, "which", lambda name: str(fake_java) if name == "java" else None)

    candidates = jvm._candidates()
    assert fake_home in candidates


def test_ensure_jdk_error_mentions_platform_install_hint(monkeypatch):
    jvm.ensure_jdk.cache_clear()
    monkeypatch.setattr(jvm, "_candidates", lambda: [])
    monkeypatch.setattr(jvm.sys, "platform", "linux")
    try:
        jvm.ensure_jdk()
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "apt install" in str(exc)
    jvm.ensure_jdk.cache_clear()
