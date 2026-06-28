from pathlib import Path

from infogrep.config import Config, index_home


def test_index_is_outside_the_target(tmp_path: Path):
    cfg = Config.load(tmp_path)
    assert cfg.target_dir == tmp_path.resolve()
    # The index lives under INFOGREP_HOME (a separate place), NOT inside the target.
    assert cfg.index_dir.is_relative_to(index_home())
    assert not cfg.index_dir.is_relative_to(tmp_path.resolve())
    assert cfg.manifest_path.name == "manifest.sqlite"
    assert cfg.dense.embedder == "qwen"
    assert cfg.sparse.enabled is True
    assert cfg.dense.enabled is False  # dense is opt-in
    assert cfg.kb.enabled is False


def test_config_toml_overrides_defaults(tmp_path: Path):
    cfg0 = Config.load(tmp_path)
    cfg0.index_dir.mkdir(parents=True, exist_ok=True)
    (cfg0.index_dir / "config.toml").write_text(
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


def test_global_config_applies_then_per_index_overrides(tmp_path: Path):
    (index_home()).mkdir(parents=True, exist_ok=True)
    (index_home() / "config.toml").write_text("[dense]\nenabled = true\nembedder = 'hash'\n")
    cfg = Config.load(tmp_path)
    assert cfg.dense.enabled is True  # from global
    assert cfg.dense.embedder == "hash"
