from typer.testing import CliRunner

from infogrep import __version__
from infogrep.cli import app

runner = CliRunner()


def test_help_runs():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "index" in result.output
    assert "search" in result.output
    assert "status" in result.output


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_status_on_unindexed_dir(tmp_path):
    result = runner.invoke(app, ["status", str(tmp_path)])
    assert result.exit_code == 0
    assert "indexed: no" in result.output


class _FakeAsk:
    """Stands in for infogrep.ask.Ask, so no dsh (and no model) is involved."""

    def __init__(self, question, directory=None, session=None, notes=False):
        from infogrep.ask import AskError

        if question == "fail":
            raise AskError("DeepSeek Harness (dsh) is not installed")
        self.session, self.notes = session, notes

    def events(self):
        yield {"type": "session", "id": "session-1"}
        yield {"type": "step", "text": "Searching “budget”"}
        yield {"type": "answer",
               "text": f"1,500 yuan (session: {self.session}, notes: {self.notes}).",
               "sources": ["/docs/plan.docx"]}


def test_ask_prints_progress_the_answer_and_its_sources(monkeypatch):
    import infogrep.ask

    monkeypatch.setattr(infogrep.ask, "Ask", _FakeAsk)
    result = runner.invoke(app, ["ask", "How much is transport?"])
    assert result.exit_code == 0
    assert "[infogrep] Searching “budget”" in result.output
    # The knowledge base is searched too, unless --no-notes says not to.
    assert "1,500 yuan (session: None, notes: True).\n\nSources:\n  /docs/plan.docx" in result.output

    monkeypatch.setattr(infogrep.ask, "latest_session", lambda: "session-9")
    result = runner.invoke(app, ["ask", "--continue", "--no-notes", "And meals?"])
    assert "(session: session-9, notes: False)" in result.output  # picks up the last conversation


def test_ask_reports_why_there_is_no_answer(monkeypatch):
    import infogrep.ask

    monkeypatch.setattr(infogrep.ask, "Ask", _FakeAsk)
    result = runner.invoke(app, ["ask", "fail"])
    assert result.exit_code == 1
    assert "dsh) is not installed" in result.output
