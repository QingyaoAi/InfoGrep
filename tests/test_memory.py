"""The form memory: what is remembered about the user, how the note the user edits and
InfoGrep's record of it stay in step, and when a remembered fact is checked again."""

import json
import os
import stat
import time

from infogrep import fill, memory
from infogrep.memory import FormMemory


def _files(tmp_path, **texts):
    docs = tmp_path / "docs"
    docs.mkdir(exist_ok=True)
    for name, text in texts.items():
        (docs / name).write_text(text)
    return docs


def _found(docs, name, value, file, **extra):
    return {"name": name, "value": value, "source": str(docs / file), **extra}


def test_facts_found_in_files_are_written_to_the_note(tmp_path):
    docs = _files(tmp_path, **{"cv.md": "x", "id.md": "y"})
    mem = FormMemory.load()
    assert mem.note == fill.fill_home() / "表单记忆.md"  # no vault: kept under INFOGREP_HOME
    assert mem.facts == {}
    assert mem.learn([
        _found(docs, "姓名", "张三", "cv.md", stable=True),
        _found(docs, "职称", "副教授", "cv.md", stable=False),
        _found(docs, "身份证号", "110101199001011234", "id.md", stable=True, sensitive=True),
        {"name": "手机", "value": "186", "source": "/nowhere/at/all.pdf"},  # not a file
        {"name": "邮箱", "value": "", "source": str(docs / "cv.md")},
        "not a fact",
    ]) == 3
    note = mem.note.read_text()
    stable, changing = note.split("## 会变")
    assert "- 姓名：张三 · [cv.md](file://" in stable
    assert "- 身份证号：110101199001011234 · 不发给模型" in stable
    assert "- 职称：副教授" in changing
    assert "手机" not in note and "邮箱" not in note
    record = fill.fill_home() / "memory.json"
    assert stat.S_IMODE(record.stat().st_mode) == 0o600  # only the user reads it

    again = FormMemory.load()
    assert set(again.facts) == {"姓名", "职称", "身份证号"}
    assert again.learn([_found(docs, "姓名", "张三", "cv.md")]) == 0  # nothing new


def test_the_agent_is_told_the_facts_and_never_a_hidden_value(tmp_path):
    docs = _files(tmp_path, **{"cv.md": "x"})
    FormMemory.load().learn([
        _found(docs, "姓名", "张三", "cv.md", stable=True),
        _found(docs, "护照号", "E12345678", "cv.md", stable=True),
    ])
    brief = FormMemory.load().brief()
    lines = brief.splitlines()
    remembered = [json.loads(line) for line in lines[lines.index("Remembered:") + 1:]]
    assert remembered == [{"name": "姓名", "value": "张三"}, {"name": "护照号", "hidden": True}]
    assert "E12345678" not in brief  # hidden by its name, whatever the agent said
    assert "Nothing is remembered yet." in FormMemory(tmp_path / "n.md", tmp_path / "r", {}).brief()


def test_a_hidden_fact_is_put_in_by_the_server(tmp_path):
    docs = _files(tmp_path, **{"id.md": "x"})
    FormMemory.load().learn([_found(docs, "身份证号", "110101199001011234", "id.md")])
    mem = FormMemory.load()
    values = mem.reveal([
        {"id": "f1", "memory": "身份证号"},
        {"id": "f2", "memory": "没记过的"},  # nothing to put in: left out
        {"id": "f3", "value": "110101199001011234"},  # a remembered value, said to be one
        {"id": "f4", "value": "something else", "source": "/x.pdf"},
    ])
    assert values == [
        {"id": "f1", "value": "110101199001011234", "remembered": "身份证号",
         "source": str(docs / "id.md")},
        {"id": "f3", "value": "110101199001011234"},  # hidden facts aren't matched by value
        {"id": "f4", "value": "something else", "source": "/x.pdf"},
    ]


def test_the_users_edits_to_the_note_are_theirs(tmp_path):
    docs = _files(tmp_path, **{"cv.md": "x"})
    mem = FormMemory.load()
    mem.learn([_found(docs, "手机", "186", "cv.md"), _found(docs, "邮箱", "a@b.c", "cv.md"),
               _found(docs, "单位", "示例大学", "cv.md")])
    text = mem.note.read_text()
    text = text.replace("- 手机：186", "- 手机：139")  # changed
    text = "\n".join(line for line in text.splitlines() if not line.startswith("- 邮箱"))  # gone
    text = text.replace("## 会变\n", "## 会变\n\n- 微信号：aqy-wx\n")  # added
    mem.note.write_text(text)

    mem = FormMemory.load()
    assert set(mem.facts) == {"手机", "单位", "微信号"}
    assert mem.facts["手机"]["value"] == "139" and mem.facts["手机"]["origin"] == "you"
    assert mem.facts["微信号"]["origin"] == "you"
    assert mem.facts["单位"]["origin"] == "file"
    def always(*args):  # every fact is mentioned by newer files
        return True

    assert mem._check(mem.facts["手机"], always) == ""  # the user's word is never re-checked

    # A file that says otherwise doesn't overwrite the user's value: it is shown beside it.
    assert mem.learn([_found(docs, "手机", "186", "cv.md")]) == 1
    assert mem.facts["手机"]["value"] == "139"
    assert "- 手机：139 · 你确认的 · " in mem.note.read_text()
    assert "⚠ 文件里另有说法：186" in mem.note.read_text()
    assert FormMemory.load().learn([_found(docs, "手机", "186", "cv.md")]) == 0  # said once

    # The user takes the file's value: the conflict is over.
    mem.note.write_text(mem.note.read_text().replace("- 手机：139", "- 手机：186"))
    mem = FormMemory.load()
    assert mem.facts["手机"]["value"] == "186" and "conflict" not in mem.facts["手机"]


def test_a_value_found_anew_replaces_one_found_before(tmp_path):
    docs = _files(tmp_path, **{"old.md": "x", "new.md": "y"})
    FormMemory.load().learn([_found(docs, "职称", "讲师", "old.md")])
    mem = FormMemory.load()
    assert mem.learn([_found(docs, "职称", "副教授", "new.md")]) == 1
    assert mem.facts["职称"]["value"] == "副教授"
    assert mem.facts["职称"]["source"] == str(docs / "new.md")
    assert "之前：讲师" in mem.note.read_text()


def test_when_a_remembered_fact_is_checked_again(tmp_path):
    docs = _files(tmp_path, **{"cv.md": "x", "id.md": "y"})
    FormMemory.load().learn([
        _found(docs, "姓名", "张三", "cv.md", stable=True),
        _found(docs, "出生日期", "1990-01-01", "id.md", stable=True),
        _found(docs, "职称", "副教授", "cv.md", stable=False),
    ])
    mem = FormMemory.load()
    assert [mem._check(f, None) for f in mem.facts.values()] == ["", "", ""]

    (docs / "id.md").unlink()
    os.utime(docs / "cv.md", (1, 1))
    asked = []

    def newer(name, since, source):
        asked.append(name)
        return True

    checks = {f["name"]: mem._check(f, newer) for f in mem.facts.values()}
    assert checks == {"姓名": "the file it came from has changed",
                      "出生日期": "the file it came from is gone",
                      "职称": "the file it came from has changed"}
    assert asked == []  # a changed file says enough

    (docs / "cv.md").write_text("x")  # as it was: only newer files, or time, can say more
    mem = FormMemory.load()
    mem.learn([_found(docs, "职称", "副教授", "cv.md"), _found(docs, "姓名", "张三", "cv.md")])
    fact = mem.facts["职称"]
    assert mem._check(fact, newer) == "newer files mention it" and asked == ["职称"]
    assert mem._check(mem.facts["姓名"], newer) == ""  # stable: newer files don't matter
    fact["checked_at"] = time.time() - 200 * 86400
    fact["checked"] = time.strftime("%Y-%m-%d", time.localtime(fact["checked_at"]))
    assert mem._check(fact, None) == "it was last checked a while ago"
    brief = mem.brief(newer=lambda *a: False)
    assert '"check": "it was last checked a while ago"' in brief
    assert f'"source": "{docs / "cv.md"}"' in brief  # where to look again


def test_the_note_goes_in_the_vault_when_there_is_one(tmp_path):
    from infogrep.config import Config

    vault = tmp_path / "vault"
    vault.mkdir()
    folder = tmp_path / "indexed"
    folder.mkdir()
    cfg = Config.load(folder)
    cfg.index_dir.mkdir(parents=True)
    (cfg.index_dir / "source.txt").write_text(str(folder))
    (cfg.index_dir / "config.toml").write_text(f"[kb]\nenabled = true\nvault_path = '{vault}'\n")
    assert memory.note_path() == vault / "Agent Knowledge" / "表单记忆.md"
    assert FormMemory.load().about() == {
        "path": str(vault / "Agent Knowledge" / "表单记忆.md"), "facts": 0,
        "url": "obsidian://open?path=" + memory.quote(str(vault / "Agent Knowledge" / "表单记忆.md")),
    }


def test_names_with_a_middle_dot_survive(tmp_path):
    docs = _files(tmp_path, **{"cv.md": "x"})
    FormMemory.load().learn([_found(docs, "姓名", "爱新觉罗·玄烨", "cv.md", stable=True)])
    assert FormMemory.load().facts["姓名"]["value"] == "爱新觉罗·玄烨"


def test_newer_means_after_the_fact_was_checked_not_after_that_day_began(tmp_path):
    docs = _files(tmp_path, **{"cv.md": "x"})
    mem = FormMemory.load()
    mem.learn([_found(docs, "手机", "186", "cv.md", stable=False)])
    seen = []
    mem._check(mem.facts["手机"], lambda name, since, source: seen.append(since))
    assert time.time() - seen[0] < 60


def test_the_memory_note_is_no_source_for_itself(tmp_path):
    docs = _files(tmp_path, **{"cv.md": "x"})
    mem = FormMemory.load()
    mem.learn([_found(docs, "姓名", "张三", "cv.md")])
    assert mem.learn([{"name": "手机", "value": "186", "source": str(mem.note)}]) == 0
