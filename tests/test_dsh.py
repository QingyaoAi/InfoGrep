"""Registering InfoGrep's MCP server in DeepSeek Harness (dsh) profile patch files."""

import pytest
from typer.testing import CliRunner

from infogrep import dsh
from infogrep.cli import app

runner = CliRunner()

# What dsh writes into a fresh profile: comments, then an empty list.
TEMPLATE = (
    "# Your patch layer for this dsh profile, applied after every bundle layer:\n"
    "# a top-level YAML array of loader patch entries.\n"
    "[]\n"
)
# A patch file someone has already been using, `!!js` expression included.
IN_USE = (
    "# my overrides\n"
    "- id: agent-default-model\n"
    "  name: '@deepseek-ai/dsh-agent-default-model'\n"
    "  config:\n"
    "    model: !!js process.env.MY_MODEL || 'deepseek-flash'\n"
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A dsh home with no dsh behind it: the load check that follows every edit passes."""
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh"))
    monkeypatch.setattr(dsh, "_dsh_rejects", lambda profile: None)
    return tmp_path / "dsh"


def _profile(home, name, patch=None):
    root = home / "profiles" / name
    root.mkdir(parents=True)
    (root / "cordis.yml").write_text("[]\n")
    if patch is not None:
        (root / "cordis.patch.yml").write_text(patch)
    return root / "cordis.patch.yml"


def test_added_to_a_fresh_profile_in_place_of_the_empty_list(home, tmp_path):
    path = _profile(home, "web", TEMPLATE)
    assert dsh.status("web") == "absent"

    assert dsh.register("web", tmp_path / "docs", command="/opt/ig/bin/infogrep") == "added"
    text = path.read_text()
    assert text.startswith("# Your patch layer")  # the user's comments stay
    assert "\n[]\n" not in text  # a list can't be both `[]` and a block of items
    assert "serverName: infogrep" in text
    assert "command: '/opt/ig/bin/infogrep'" in text
    assert f"args: [ 'mcp', '--dir', '{tmp_path / 'docs'}' ]" in text
    assert dsh.status("web") == "managed"


def test_appended_after_existing_entries_without_touching_them(home, tmp_path):
    path = _profile(home, "web", IN_USE)
    assert dsh.register("web", tmp_path, command="/x/infogrep") == "added"
    text = path.read_text()
    assert text.startswith(IN_USE)  # byte for byte, `!!js` expression included
    assert text.count("- insert:") == 1


def test_registering_again_refreshes_the_same_entry(home, tmp_path):
    path = _profile(home, "web", IN_USE)
    dsh.register("web", tmp_path / "old", command="/x/infogrep")
    assert dsh.register("web", tmp_path / "old", command="/x/infogrep") == "unchanged"

    assert dsh.register("web", tmp_path / "new", command="/y/infogrep") == "updated"
    text = path.read_text()
    assert text.count("serverName: infogrep") == 1  # dsh rejects a duplicate server name
    assert "/y/infogrep" in text and "/x/infogrep" not in text
    assert str(tmp_path / "new") in text


def test_a_hand_written_infogrep_server_is_left_alone(home, tmp_path):
    manual = IN_USE + (
        "- insert:\n"
        "    - id: mcp-infogrep\n"
        "      name: '@deepseek-ai/dsh-mcp-client'\n"
        "      config:\n"
        "        serverName: infogrep\n"
        "        command: '/mine/infogrep'\n"
    )
    path = _profile(home, "web", manual)
    assert dsh.status("web") == "manual"
    assert dsh.register("web", tmp_path, command="/x/infogrep") == "manual"
    assert dsh.unregister("web") == "manual"
    assert path.read_text() == manual


def test_unregister_restores_the_file(home, tmp_path):
    path = _profile(home, "web", IN_USE)
    dsh.register("web", tmp_path, command="/x/infogrep")
    assert dsh.unregister("web") == "removed"
    assert path.read_text().rstrip("\n") == IN_USE.rstrip("\n")
    assert dsh.unregister("web") == "absent"

    # Removing the only entry must leave a list behind, not an empty document.
    fresh = _profile(home, "headless", TEMPLATE)
    dsh.register("headless", tmp_path, command="/x/infogrep")
    dsh.unregister("headless")
    assert fresh.read_text().rstrip("\n").endswith("[]")
    assert dsh._entries(fresh.read_text()) == ["[]"]


def test_paths_with_quotes_and_spaces_survive_as_yaml(home, tmp_path):
    path = _profile(home, "web", TEMPLATE)
    dsh.register("web", tmp_path / "Bob's files", command="/Agent Tools/it's/infogrep")
    text = path.read_text()
    assert "command: '/Agent Tools/it''s/infogrep'" in text  # '' is YAML's escaped quote
    assert "Bob''s files' ]" in text


def test_unknown_profile_and_unfamiliar_layouts_change_nothing(home, tmp_path):
    with pytest.raises(FileNotFoundError, match="no dsh profile named 'tui'"):
        dsh.register("tui", tmp_path)

    flow = "[ { id: x, name: y } ]\n"  # a flow-style list can't simply be appended to
    path = _profile(home, "web", flow)
    with pytest.raises(ValueError, match="by hand"):
        dsh.register("web", tmp_path, command="/x/infogrep")
    assert path.read_text() == flow


def test_an_edit_dsh_cannot_load_is_rolled_back(home, tmp_path, monkeypatch):
    path = _profile(home, "web", IN_USE)
    monkeypatch.setattr(dsh, "_dsh_rejects", lambda profile: "YAMLException: bad indentation")
    with pytest.raises(ValueError, match="restored"):
        dsh.register("web", tmp_path, command="/x/infogrep")
    assert path.read_text() == IN_USE


def test_cli_install_list_uninstall(home, tmp_path):
    _profile(home, "web", TEMPLATE)
    _profile(home, "headless", TEMPLATE)

    result = runner.invoke(app, ["dsh", "install", "--dir", str(tmp_path)])
    assert result.exit_code == 0 and "InfoGrep added" in result.output
    assert dsh.status("web") == "managed"  # the default profile...
    assert dsh.status("headless") == "absent"  # ...and only that one

    listing = runner.invoke(app, ["dsh", "list"]).output
    assert "web" in listing and "yes" in listing and "headless" in listing

    missing = runner.invoke(app, ["dsh", "install", "--dir", str(tmp_path), "--profile", "tui"])
    assert missing.exit_code == 1 and "no dsh profile named 'tui'" in missing.output

    result = runner.invoke(app, ["dsh", "uninstall"])
    assert result.exit_code == 0 and "InfoGrep removed" in result.output
    assert dsh.status("web") == "absent"
