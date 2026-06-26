from pathlib import Path

from infogrep.config import Config, SIDECAR_DIRNAME


def test_defaults_and_sidecar_paths(tmp_path: Path):
    cfg = Config.load(tmp_path)
    assert cfg.target_dir == tmp_path.resolve()
    assert cfg.sidecar_dir == tmp_path.resolve() / SIDECAR_DIRNAME
    assert cfg.manifest_path.name == "manifest.sqlite"
    assert cfg.dense.embedder == "qwen"
    assert cfg.sparse.enabled is True
    assert cfg.kb.enabled is False


def test_config_toml_overrides_defaults(tmp_path: Path):
    sidecar = tmp_path / SIDECAR_DIRNAME
    sidecar.mkdir()
    (sidecar / "config.toml").write_text(
        "\n".join(
            [
                "exclude = ['secret/**']",
                "[chunk]",
                "size = 256",
                "[dense]",
                "embedder = 'harrier'",
                "[kb]",
                "enabled = true",
                "vault = 'My Vault'",
            ]
        )
    )
    cfg = Config.load(tmp_path)
    assert cfg.exclude == ["secret/**"]
    assert cfg.chunk.size == 256
    assert cfg.chunk.overlap == 64  # untouched default preserved
    assert cfg.dense.embedder == "harrier"
    assert cfg.kb.enabled is True
    assert cfg.kb.vault == "My Vault"
