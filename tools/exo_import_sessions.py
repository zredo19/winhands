"""Import the conversations Exo had before it kept a history into ~/.exo/chats (run it once; reruns add nothing).

    python tools/exo_import_sessions.py

Exo runs Claude Code with cwd ~/.exo, so every conversation is a Claude Code session in
~/.claude/projects/C--Users-<you>--exo/<session>.jsonl. Only the sessions exo.log shows the app opened are
imported (dev scripts also ran there), rebuilt as the bubbles Exo showed: the spoken acknowledgement and the
final answer of each turn.
"""
import json, pathlib, re, sys
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from winhands.exo.brain import HOME, first_sentence, spoken, without_ack

SESSIONS = pathlib.Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(HOME))
CONTEXT = re.compile(r"^\[The user was in the window .*?\]\n", re.S)


def _ms(stamp):
    return int(datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() * 1000)


def chat_from_session(path):
    """A Claude Code session file -> an Exo chat ({id, title, session, messages}), or None if nobody spoke."""
    turns = []  # [user text, its time, [(assistant text, time), ...]]
    for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
        try:
            m = json.loads(line)
        except ValueError:
            continue
        content = m.get("message", {}).get("content") if isinstance(m.get("message"), dict) else None
        if m.get("type") == "user" and isinstance(content, str) and not content.startswith("[Request interrupted"):
            turns.append([CONTEXT.sub("", content).strip(), m["timestamp"], []])
        elif m.get("type") == "assistant" and turns and isinstance(content, list):
            turns[-1][2] += [(c["text"], m["timestamp"]) for c in content if c.get("type") == "text"]
    messages = []
    for text, at, replies in turns:
        messages.append({"who": "user", "text": text, "at": _ms(at)})
        if not replies:
            continue
        ack = first_sentence(replies[0][0])
        if ack:
            messages.append({"who": "exo", "text": spoken(ack), "kind": "ack", "at": _ms(replies[0][1])})
        final = without_ack(spoken(replies[-1][0]), spoken(ack) if ack else None)
        if final or not ack:
            messages.append({"who": "exo", "text": final or "Listo.", "ok": True, "at": _ms(replies[-1][1])})
    if not messages:
        return None
    return {"id": str(messages[0]["at"] * 1_000_000), "title": messages[0]["text"][:80],
            "session": pathlib.Path(path).stem, "messages": messages}


def import_sessions(sessions=SESSIONS, exo_log=HOME / "exo.log", chats=HOME / "chats"):
    """Returns how many chats it added."""
    opened = set(re.findall(r"brain ready: (\S+)", pathlib.Path(exo_log).read_text(encoding="utf-8", errors="replace")))
    chats = pathlib.Path(chats)
    chats.mkdir(parents=True, exist_ok=True)
    have = set()
    for p in chats.glob("*.json"):
        try:
            have.add(json.loads(p.read_text(encoding="utf-8")).get("session"))
        except (OSError, ValueError):
            pass
    added = 0
    for f in pathlib.Path(sessions).glob("*.jsonl"):
        if f.stem not in opened or f.stem in have:
            continue
        chat = chat_from_session(f)
        if chat:
            (chats / f"{chat['id']}.json").write_text(json.dumps(chat, ensure_ascii=False), encoding="utf-8")
            added += 1
    return added


if __name__ == "__main__":
    print(f"imported {import_sessions()} chats into {HOME / 'chats'}")
