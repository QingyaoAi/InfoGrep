"""Filling in a web form: what the agent is shown of the page's fields, how it is set to
work, and which of its reply is taken — against a stand-in ``dsh`` so no model is called."""

import json
import os
import stat

import pytest

from infogrep import ask, fill

# A stand-in for `dsh`: records how it was called, then replies with values for a form.
FAKE_DSH = r'''#!/usr/bin/env python3
import json, os, sys

task = sys.stdin.read()
with open(os.environ["FAKE_DSH_RECORD"], "w") as fh:
    json.dump({"args": sys.argv[1:], "task": task}, fh)

def emit(**event):
    print(json.dumps(event), flush=True)

emit(type="session", sessionId="session-form")
emit(type="tool_call", callId="1", tool="mcp__infogrep__search_sparse", input={"query": "护照"})
emit(type="final", text=os.environ["FAKE_DSH_REPLY"])
'''

FIELDS = [
    {"id": "f1", "kind": "text", "label": "姓名", "section": "Applicant"},
    {"id": "f2", "kind": "select", "label": "Country", "options": ["China", "France"]},
    {"id": "f3", "kind": "checkbox", "label": "I hold a passport"},
    {"id": "f4", "kind": "multiselect", "label": "Languages", "options": ["中文", "English"]},
    {"id": "f5", "kind": "email", "label": "Email", "value": "me@example.org"},
]


@pytest.fixture
def fake_dsh(tmp_path, monkeypatch):
    """Put the stand-in first on PATH; returns where it records its call."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "dsh"
    exe.write_text(FAKE_DSH)
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    record = tmp_path / "dsh-call.json"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DSH_RECORD", str(record))
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh-home"))
    reply = {"fields": [{"id": "f1", "value": "张三", "source": "/docs/cv.pdf"}],
             "note": "No passport number in the files."}
    monkeypatch.setenv("FAKE_DSH_REPLY", json.dumps(reply, ensure_ascii=False))
    return record


# -- what the agent is shown ------------------------------------------------------


def test_fields_keep_what_describes_them_and_lose_the_rest():
    fields = fill.clean_fields([
        {"id": "f1", "kind": "text", "label": "  Full\n name ", "n": 3, "onclick": "x()",
         "maxlength": 40, "required": True, "value": ""},
        {"id": "f1", "label": "the same id again"},
        {"id": "bad id!", "label": "an id that is not one"},
        {"label": "no id at all"},
        "not a field",
        {"id": "f2", "kind": "checkbox", "label": "Agree", "value": False, "required": "yes"},
        {"id": "f3", "kind": "select", "label": "x" * 1000,
         "options": ["A", "", 7, *(f"o{i}" for i in range(400))]},
    ])

    assert [f["id"] for f in fields] == ["f1", "f2", "f3"]
    assert fields[0] == {"id": "f1", "kind": "text", "label": "Full name", "required": True,
                         "maxlength": 40}
    assert fields[1] == {"id": "f2", "kind": "checkbox", "label": "Agree", "value": False}
    assert len(fields[2]["label"]) == 300 and fields[2]["label"].endswith("…")
    # The first MAX_OPTIONS, less the blank one and the one that is not a text.
    assert fields[2]["options"][:2] == ["A", "o0"]
    assert len(fields[2]["options"]) == fill.MAX_OPTIONS - 2
    assert fill.clean_fields("not a list") == [] and fill.clean_fields(None) == []


def test_a_long_form_is_cut_short():
    many = [{"id": f"f{i}", "label": f"Field {i}"} for i in range(fill.MAX_FIELDS + 50)]
    assert len(fill.clean_fields(many)) == fill.MAX_FIELDS

    wide = [{"id": f"f{i}", "label": "x" * 300, "options": ["y" * 80] * 300} for i in range(50)]
    kept = fill.clean_fields(wide)
    assert 0 < len(kept) < 50
    assert sum(len(json.dumps(f, ensure_ascii=False)) for f in kept) <= fill._FORM


def test_the_form_reads_as_the_page_the_note_and_a_line_per_field():
    text = fill.form_text({"title": "Visa application", "url": "https://visa.example/apply"},
                          FIELDS[:2], note="Use my office address.")
    lines = text.splitlines()
    assert lines[0] == "The page: Visa application — https://visa.example/apply"
    assert lines[1] == "The user's note: Use my office address."
    assert lines[2] == "The fields:"
    assert [json.loads(line) for line in lines[3:]] == FIELDS[:2]
    assert "姓名" in text  # as written, not \u-escaped

    again = fill.form_text(None, FIELDS[:1], again=True)
    assert again.startswith(fill._AGAIN) and "The page:" not in again


def test_the_overlay_keeps_form_conversations_apart_and_the_session_log_home():
    text = fill.patch_text()
    assert f"root: '{fill.fill_home() / 'sessions'}'" in text
    assert "- id: session-log-deepseek\n" in text and "enabled: false" in text


# -- what the agent replies -------------------------------------------------------


def test_values_are_taken_for_the_fields_the_form_has():
    reply = """Here is what I found:
```json
{"fields": [
  {"id": "f1", "value": " 张三 ", "source": "/docs/cv.pdf"},
  {"id": "f1", "value": "a second value for the same field"},
  {"id": "f9", "value": "a field the form does not have"},
  {"id": ["f2"], "value": "an id that is not one"},
  {"id": "f2", "value": "Germany"},
  {"id": "f3", "value": "yes"},
  {"id": "f4", "value": "English"},
  {"id": "f5", "value": ""},
  "not an entry"
],
"note": "No passport number in the files."}
```"""
    values, note = fill.parse(reply, FIELDS)

    assert values == [
        {"id": "f1", "value": "张三", "source": "/docs/cv.pdf"},
        {"id": "f2", "value": "Germany"},  # whether it is an option is for the page to say
        {"id": "f3", "value": True},
        {"id": "f4", "value": ["English"]},
    ]
    assert note == "No passport number in the files."


def test_what_a_field_takes():
    fit = fill._fit
    tick = {"kind": "checkbox"}
    assert (fit(True, tick), fit("否", tick), fit("maybe", tick), fit(1, tick)) == (
        True, False, None, None)
    several = {"kind": "multiselect"}
    assert fit(["中文", " ", 3, "English"], several) == ["中文", "English"]
    assert fit([], several) is None
    text = {"kind": "number"}
    assert (fit(42, text), fit(" x ", text), fit(True, text), fit({"a": 1}, text)) == (
        "42", "x", None, None)


def test_a_reply_that_is_not_the_json_asked_for_is_an_error():
    with pytest.raises(ask.AskError, match="did not reply with values, but with: Sorry"):
        fill.parse("Sorry, I could not find anything about you.", FIELDS)
    with pytest.raises(ask.AskError):
        fill.parse('{"values": []}', FIELDS)
    assert fill.parse('{"fields": []}', FIELDS) == ([], "")


# -- one form ---------------------------------------------------------------------


def test_a_form_runs_the_answering_agent_with_the_form_filling_overlay(tmp_path, fake_dsh):
    run = fill.start({"title": "Visa", "url": "https://visa.example/"}, FIELDS,
                     note="For me.", directory=tmp_path, server="http://127.0.0.1:7421")
    events = list(run.events())

    assert events[0] == {"type": "session", "id": "session-form"}
    assert events[1] == {"type": "step", "text": "Searching “护照”"}
    values, note = fill.parse(events[-1]["text"], FIELDS)
    assert values == [{"id": "f1", "value": "张三", "source": "/docs/cv.pdf"}]
    assert note == "No passport number in the files."

    call = json.loads(fake_dsh.read_text())
    overlay = fill.fill_home() / "dsh.patch.yml"
    own = ask.ask_home() / "dsh.patch.yml"
    assert call["args"] == ["--profile", "headless", "--patch", str(own),
                            "--patch", str(overlay), "--json", "-"]
    assert overlay.read_text() == fill.patch_text()
    assert call["task"].startswith("You are filling in a form on a web page for the user.")
    assert f"Searches cover the folder {tmp_path}." in call["task"]
    form = fill.form_text({"title": "Visa", "url": "https://visa.example/"}, FIELDS, note="For me.")
    assert call["task"].endswith(form + "\n")


def test_a_later_form_goes_on_with_the_conversation(fake_dsh):
    list(fill.start(None, FIELDS[:1], session="session-form").events())

    call = json.loads(fake_dsh.read_text())
    assert call["args"][-3:] == ["--session-id", "session-form", "-"]
    assert call["task"] == fill.form_text(None, FIELDS[:1], again=True)  # no instructions again


# -- lists and prose --------------------------------------------------------------


def test_a_cascader_takes_a_path():
    path = {"kind": "cascader"}
    assert fill._fit(["北京市", " 海淀区 ", ""], path) == ["北京市", "海淀区"]
    assert fill._fit("北京市 / 海淀区", path) == ["北京市", "海淀区"]
    assert fill._fit("上海市＞徐汇区", path) == ["上海市", "徐汇区"]


def test_a_later_round_says_the_form_changed_and_what_was_tried():
    fields = fill.clean_fields([{"id": "f7", "kind": "select", "label": "城市",
                                 "options": ["海淀区", "朝阳区"], "tried": "北京"}])
    assert fields[0]["tried"] == "北京"
    text = fill.form_text(None, fields, again=True, changed=True)
    assert text.startswith(fill._CHANGED)
    assert fill.form_text(None, fields, changed=True) == fill.form_text(None, fields)


def _indexed(folder):
    """Make ``folder`` one InfoGrep has an index for, as far as indexed_dirs() can tell."""
    entry = fill.ask.index_home() / "indexes" / "docs-test"
    entry.mkdir(parents=True)
    (entry / "source.txt").write_text(str(folder))


def test_prose_goes_in_only_when_its_source_has_it_word_for_word(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    _indexed(docs)
    (docs / "cv.md").write_text("# CV\n\nZhang San works on information\nretrieval and "
                                "machine learning.\n\n研究方向：信息检索。")
    outside = tmp_path / "elsewhere.md"
    outside.write_text("Zhang San works on information retrieval and machine learning.")
    fields = [{"id": "f1", "kind": "richtext"}, {"id": "f2", "kind": "text"}]
    values = fill.mark_copied([
        {"id": "f1", "value": "Zhang San works on information retrieval and machine learning.",
         "source": str(docs / "cv.md")},
        {"id": "f2", "value": "not prose, not checked"},
    ], fields)
    assert values[0]["copied"] is True  # the line break in the file is no difference
    assert "copied" not in values[1]

    def copied(value, source):
        entry = {"id": "f1", "value": value}
        if source:
            entry["source"] = str(source)
        return fill.mark_copied([entry], fields)[0]["copied"]

    assert copied("研究方向： 信息检索。", docs / "cv.md")  # full-width colon, a space
    assert not copied("Zhang San is a leading researcher in retrieval.", docs / "cv.md")
    assert not copied("Zhang San works on information retrieval and machine learning.", outside)
    assert not copied("Zhang San works on information retrieval.", None)
    assert not copied("Zhang San works", docs / "missing.md")


# -- the memory ---------------------------------------------------------------------


def test_a_field_can_take_a_remembered_fact_and_the_reply_can_report_facts():
    reply = json.dumps({"fields": [
        {"id": "f1", "memory": "身份证号"},
        {"id": "f2", "value": "", "memory": ""},  # neither: nothing for the field
    ], "facts": [{"name": "手机", "value": "186", "source": "/cv.pdf"}]})
    values, _ = fill.parse(reply, FIELDS)
    assert values == [{"id": "f1", "memory": "身份证号"}]
    assert fill.parse_facts(reply) == [{"name": "手机", "value": "186", "source": "/cv.pdf"}]
    assert fill.parse_facts('{"fields": [], "facts": "none"}') == []


def test_what_is_remembered_opens_the_form():
    text = fill.form_text(None, FIELDS[:1], memory="Remembered:\n{\"name\": \"姓名\"}")
    assert text.startswith("Remembered:") and text.index("姓名") < text.index("The fields:")
