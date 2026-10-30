"""会话存储：data/sessions/<id>.json，一个文件一个会话。

每个会话带 owner（账号 id）。列表/读取都会按 owner 过滤，
所以不同账号只看得到自己的对话。
"""
import json
import os
import time
import uuid

from config import DATA_DIR

SESS_DIR = os.path.join(DATA_DIR, "sessions")


def _path(sid):
    return os.path.join(SESS_DIR, f"{sid}.json")


def _ensure():
    os.makedirs(SESS_DIR, exist_ok=True)


def new_session(title="新对话", owner=None):
    _ensure()
    sid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    data = {
        "id": sid,
        "owner": owner,
        "title": title,
        "created": time.time(),
        "updated": time.time(),
        "summary": "",
        "summary_upto": 0,  # summary 已覆盖到 messages 的前 N 条
        "messages": [],
    }
    save(data)
    return data


def save(session):
    _ensure()
    session["updated"] = time.time()
    tmp = _path(session["id"]) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(session, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _path(session["id"]))


def load(sid, owner=None):
    """读取会话；给了 owner 就只返回属于他的（owner 为空的历史会话归第一个账号）。"""
    try:
        with open(_path(sid), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if owner and data.get("owner") not in (None, owner):
        return None
    return data


def delete(sid, owner=None):
    s = load(sid, owner)
    if s is None:
        return False
    try:
        os.remove(_path(sid))
        return True
    except OSError:
        return False


def list_sessions(owner=None):
    _ensure()
    items = []
    for name in os.listdir(SESS_DIR):
        if not name.endswith(".json"):
            continue
        sid = name[:-5]
        s = load(sid, owner)
        if not s:
            continue
        msgs = s.get("messages") or []
        last = ""
        for m in reversed(msgs):
            if m.get("content"):
                last = m["content"][:60]
                break
        items.append(
            {
                "id": s["id"],
                "title": s.get("title") or "未命名",
                "updated": s.get("updated") or 0,
                "turns": sum(1 for m in msgs if m.get("role") == "user"),
                "preview": last,
                "has_summary": bool(s.get("summary")),
            }
        )
    items.sort(key=lambda x: x["updated"], reverse=True)
    return items


def count_owned(owner):
    return sum(1 for name in os.listdir(SESS_DIR) if name.endswith(".json")
               and (load(name[:-5]) or {}).get("owner") == owner) if os.path.isdir(SESS_DIR) else 0


def claim_ownerless(owner):
    """把没有归属的历史会话划给某个账号（首次启用账号时调用）。返回认领数量。"""
    _ensure()
    n = 0
    for name in os.listdir(SESS_DIR):
        if not name.endswith(".json"):
            continue
        s = load(name[:-5])
        if s and not s.get("owner"):
            s["owner"] = owner
            save(s)
            n += 1
    return n


def append_message(session, role, content, extra=None):
    msg = {"role": role, "content": content, "ts": time.time()}
    if extra:
        msg.update(extra)
    session.setdefault("messages", []).append(msg)
    if role == "user" and (not session.get("title") or session["title"] == "新对话"):
        session["title"] = content.strip().replace("\n", " ")[:24] or "新对话"
    return msg


def truncate_last_assistant(session):
    """重新生成前，把最后一条 assistant 回复摘掉。"""
    msgs = session.get("messages") or []
    if msgs and msgs[-1].get("role") == "assistant":
        msgs.pop()
        return True
    return False
