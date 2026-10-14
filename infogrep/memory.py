"""What form filling remembers about the user, so that the next form needn't search again.

It remembers facts, not form fields: "手机", "工作单位", "职称", each with its value, the
file it was found in, and when. Forms word the same fact differently ("联系电话",
"Mobile"); the agent maps a form's fields onto the facts.

The facts live in a note the user reads and edits: ``<vault>/<agent folder>/表单记忆.md``
when an indexed folder has a knowledge base (its vault), else the same note under
``$INFOGREP_HOME/ask/fill``. A line the user changes or adds counts as theirs, and a
file saying otherwise later does not overwrite it; deleting a line forgets the fact.
Beside it, ``memory.json`` keeps what only InfoGrep needs: the value it wrote last (which
is how a change of the user's shows), and the state of the file each fact came from.

A remembered fact can go out of date, so a fact is offered to the agent with "check" when
its file changed or was removed, and a fact that changes over time (a title, a phone)
also when newer files mention it, or when it was last checked half a year ago. Some
facts are too sensitive to send to the model with every form (ID, passport and bank
numbers): the agent is only told that they are remembered, and the server puts the value
in when the agent names the fact for a field.
"""

from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

from .config import Config, indexed_dirs
from .retrieval.kb import _DATALESS, save_text, vault_root

NOTE_NAME = "表单记忆.md"
MAX_FACTS = 200
RECHECK_DAYS = 180  # a fact that changes over time is checked again after this long
_STABLE, _CHANGING = "不常变", "会变"
_SENSITIVE = re.compile(
    r"身份证|证件号|护照|通行证|银行卡|卡号|银行账号|社保|医保|公积金|passport|\bid\s*(card|number|no)\b|"
    r"social security|\bssn\b|bank\s*(card|account)",
    re.IGNORECASE,
)
_LINE = re.compile(r"^\s*[-*]\s+(.+?)\s*[：:]\s*(.*?)\s*$")
_SEP = " · "

_INTRO = (
    "InfoGrep 填表时记下的你的信息，下次填表直接用，不必再去文件里查。可以直接修改："
    "改过或新加的一行算作你确认的，文件里出现不同的说法也不会覆盖它；删掉一行就忘掉它。"
    f"「{_CHANGING}」里的信息，用之前会先看有没有更新的文件提到它。"
)


def _key(name: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", name).split()).casefold()


def _clean(text) -> str:
    return " ".join(str(text).split()) if isinstance(text, (str, int, float)) else ""


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _stamp(path: str) -> list[int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return [st.st_mtime_ns, st.st_size]


def note_path() -> Path:
    """Where the memory note is: in the vault of the first indexed folder that has a
    knowledge base, else under ``$INFOGREP_HOME/ask/fill``."""
    from .fill import fill_home

    for folder in indexed_dirs():
        kb = Config.load(folder).kb
        root = vault_root(kb) if kb.enabled else None
        if root is not None and root.is_dir():
            return root / kb.agent_folder.strip("/") / NOTE_NAME
    return fill_home() / NOTE_NAME


def _parse_note(text: str) -> list[dict]:
    """The facts a memory note lists: ``- name：value · notes`` lines, by section."""
    facts, stable = [], True
    for line in text.splitlines():
        if line.startswith("## "):
            stable = _CHANGING not in line
            continue
        m = _LINE.match(line)
        if m:
            value = m.group(2).split(_SEP, 1)[0]  # what follows is InfoGrep's notes on it
            if _clean(m.group(1)) and _clean(value):
                facts.append({"name": _clean(m.group(1)), "value": _clean(value), "stable": stable})
    return facts


class FormMemory:
    """The facts remembered, as read from the note and its record; see the module."""

    def __init__(self, note: Path, record: Path, facts: dict[str, dict]):
        self.note = note
        self.record = record
        self.facts = facts  # key -> {name, value, source, stamp, checked, origin, stable, ...}

    # -- reading ---------------------------------------------------------------

    @classmethod
    def load(cls, note: Path | None = None) -> FormMemory:
        """The memory as it stands, the user's edits to the note taken in."""
        from .fill import fill_home

        note = note or note_path()
        record = fill_home() / "memory.json"
        try:
            kept = json.loads(record.read_text(encoding="utf-8")).get("facts", {})
        except (OSError, ValueError, AttributeError):
            kept = {}
        try:
            # A note iCloud has moved off this Mac would block on a download: go by the
            # record until it is back.
            listed = None if getattr(os.stat(note), "st_flags", 0) & _DATALESS \
                else _parse_note(note.read_text(encoding="utf-8"))
        except OSError:
            listed = None
        if listed is None:
            return cls(note, record, kept if note.exists() else {})
        facts: dict[str, dict] = {}
        for item in listed[:MAX_FACTS]:
            key = _key(item["name"])
            fact = dict(kept.get(key) or {"origin": "you", "checked": _today()})
            if fact.get("value") is not None and fact["value"] != item["value"]:
                # Changed in the note since InfoGrep wrote it: the user's now.
                fact.update(origin="you", checked=_today())
                fact.pop("conflict", None)
            fact.update(name=item["name"], value=item["value"], stable=item["stable"])
            fact.setdefault("hidden", bool(_SENSITIVE.search(item["name"])))
            facts[key] = fact
        return cls(note, record, facts)

    # -- what the agent is told ------------------------------------------------

    def _check(self, fact: dict, newer: Callable[[str, float, str], bool] | None) -> str:
        """Why a fact may be out of date, or ''."""
        if fact.get("origin") == "you":
            return ""  # the user's word stands
        source = fact.get("source")
        if source:
            now = _stamp(source)
            if now is None:
                return "the file it came from is gone"
            if fact.get("stamp") and now != fact["stamp"]:
                return "the file it came from has changed"
        if fact.get("stable"):
            return ""
        since = fact.get("checked_at")
        if since is None:  # a fact the user added: from the day it was put in
            try:
                since = time.mktime(time.strptime(fact.get("checked", ""), "%Y-%m-%d"))
            except ValueError:
                since = 0.0
        if time.time() - since > RECHECK_DAYS * 86400:
            return "it was last checked a while ago"
        if newer is not None and newer(fact["name"], since, source or ""):
            return "newer files mention it"
        return ""

    def brief(self, newer: Callable[[str, float, str], bool] | None = None) -> str:
        """What the agent is told about the memory: how to use it, and the facts."""
        lines = [
            (
                "What earlier forms found out about the user is remembered: the facts below, "
                "one per line, each found in the user's files and checked there already. Take "
                "them from here: don't search for them, and don't read their files again. Give "
                "each the way its field wants it, as any value (in pinyin, in the field's date "
                "format, as one of its options). "
                "Search only for what this form asks that is not remembered, and for a fact "
                'marked "check", which may be out of date: look at its "source" again, or at '
                'newer files. One with "hidden": true is remembered but not shown to you; to '
                'put it in a field, reply with {"id": "f3", "memory": "<its name>"} in place of '
                "a value."
            ),
            (
                'Besides "fields", reply with "facts": each fact about the user that you found in '
                'a file this time, or checked again because it was marked, as {"name": "手机", '
                '"value": "...", "source": "/absolute/path", "stable": true or false, '
                '"sensitive": true or false}. '
                "Name a fact by what it is, not by this form's label (\"手机\", not \"联系电话\"), "
                "using the name it is remembered by if it is. \"stable\" is for what doesn't "
                "change over time (name, date of birth, ID numbers, degrees); \"sensitive\" for "
                "ID, passport, bank-card and similar numbers. Leave out facts about anyone else, "
                "and report none when the form is not about the user."
            ),
        ]
        shown = []
        for fact in self.facts.values():
            entry: dict = {"name": fact["name"]}
            if fact.get("hidden"):
                entry["hidden"] = True
            else:
                entry["value"] = fact["value"]
            why = self._check(fact, newer)
            if why:
                entry["check"] = why
                if fact.get("source"):
                    entry["source"] = fact["source"]
            shown.append(json.dumps(entry, ensure_ascii=False))
        lines.append("Remembered:" if shown else "Nothing is remembered yet.")
        return "\n".join(lines + shown)

    # -- what the agent replies ------------------------------------------------

    def reveal(self, values: list[dict]) -> list[dict]:
        """Values with the facts the agent named put in (and those it can't, dropped);
        a value that is a remembered fact's is said to be one, with that fact's file."""
        out = []
        for entry in values:
            name = entry.pop("memory", None)
            fact = self.facts.get(_key(name)) if isinstance(name, str) else None
            if "value" not in entry:
                if fact is None:
                    continue
                entry["value"] = fact["value"]
            elif fact is None:
                fact = next((f for f in self.facts.values() if not f.get("hidden") and
                             _key(str(entry["value"])) == _key(f["value"])), None)
            if fact is not None:
                entry["remembered"] = fact["name"]
                if fact.get("source") and not entry.get("source"):
                    entry["source"] = fact["source"]
            out.append(entry)
        return out

    def learn(self, reported) -> int:
        """Take in the facts the agent found in files; returns how many changed. Writes
        the note and its record when anything did, a check made again included."""
        changed = 0
        checked = False  # facts checked again and found as they were: only the record knows
        for item in reported if isinstance(reported, list) else []:
            if not isinstance(item, dict):
                continue
            name, value, source = _clean(item.get("name")), _clean(item.get("value")), \
                _clean(item.get("source"))
            if not name or not value or not os.path.isabs(source) or not os.path.isfile(source):
                continue  # a fact comes from a file, or it isn't one to remember
            if os.path.realpath(source) == os.path.realpath(self.note):
                continue  # and from one of the user's files, not from this memory
            key = _key(name)
            fact = self.facts.get(key)
            found = {"source": source, "stamp": _stamp(source), "checked": _today(),
                     "checked_at": time.time()}
            if fact is None:
                if len(self.facts) >= MAX_FACTS:
                    continue
                self.facts[key] = {
                    "name": name, "value": value, "origin": "file", **found,
                    "stable": item.get("stable") is True,
                    "hidden": item.get("sensitive") is True or bool(_SENSITIVE.search(name)),
                }
            elif fact.get("origin") == "you":
                if _key(value) == _key(fact["value"]):
                    if fact.pop("conflict", None) is None:
                        continue
                elif fact.get("conflict", {}).get("value") == value:
                    continue
                else:
                    fact["conflict"] = {"value": value, "source": source}
            elif _key(value) == _key(fact["value"]):
                checked = True
                if fact.get("stamp") == found["stamp"] and fact.get("checked") == _today():
                    fact.update(found)  # nothing that the note shows has changed
                    continue
                fact.update(found)
            else:
                fact.update(found, before=fact["value"], value=value)
            changed += 1
        if changed or checked:
            self.save()
        return changed

    # -- writing ---------------------------------------------------------------

    def _line(self, fact: dict) -> str:
        bits = [f"- {fact['name']}：{fact['value']}"]
        if fact.get("hidden"):
            bits.append("不发给模型")
        if fact.get("origin") == "you":
            bits.append("你确认的")
        if fact.get("source"):
            bits.append(f"[{os.path.basename(fact['source'])}](file://{quote(fact['source'])})")
        if fact.get("checked"):
            bits.append(fact["checked"])
        if fact.get("before"):
            bits.append(f"之前：{fact['before']}")
        conflict = fact.get("conflict")
        if conflict:
            where = f"[{os.path.basename(conflict['source'])}](file://{quote(conflict['source'])})"
            bits.append(f"⚠ 文件里另有说法：{conflict['value']}（{where}），要用它就改掉这一行")
        return _SEP.join(bits)

    def render(self) -> str:
        lines = ["---", "tags:", "  - infogrep/form-memory", "---", "", "# 表单记忆", "",
                 _INTRO, ""]
        for title, stable in ((_STABLE, True), (_CHANGING, False)):
            lines += [f"## {title}", ""]
            lines += [self._line(f) for f in self.facts.values() if bool(f.get("stable")) == stable]
            lines.append("")
        return "\n".join(lines)

    def save(self) -> None:
        text = self.render()
        try:
            same = self.note.read_text(encoding="utf-8") == text
        except OSError:
            same = False
        if not same:  # a note rewritten as it was would only be synced again for nothing
            save_text(self.note, text)
        self.record.parent.mkdir(parents=True, exist_ok=True)
        save_text(self.record, json.dumps({"facts": self.facts}, ensure_ascii=False, indent=1))
        os.chmod(self.record, 0o600)

    def about(self) -> dict:
        """For the extension: where the note is, how many facts it holds, how to open it."""
        from .fill import fill_home

        in_vault = self.note.parent != fill_home()
        return {
            "path": str(self.note), "facts": len(self.facts),
            "url": f"obsidian://open?path={quote(str(self.note))}" if in_vault else "",
        }
