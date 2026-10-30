"""上下文记忆：滑动窗口 + 滚动摘要。

"根据之前的对话完善回答" 分两层落地：
1. 窗口记忆：每次请求都带上最近 N 轮原文，模型能接着上文说。
2. 长期记忆：超出窗口的旧对话压缩成一段摘要，常驻 system prompt；
   摘要随对话推进滚动更新（summary + summary_upto 标记）。
"""
import re


def _turns_of(messages):
    return sum(1 for m in messages if m.get("role") == "user")


def build_context(session, cfg, extra_system=""):
    """组装发给引擎的 messages。"""
    msgs = session.get("messages") or []
    mem = cfg.get("memory") or {}
    window_turns = max(0, int(mem.get("window_turns", 6)))
    used = msgs[-window_turns * 2:] if window_turns else []

    system_parts = []
    base = (cfg.get("system_prompt") or "").strip()
    if base:
        system_parts.append(base)
    if extra_system:
        system_parts.append(extra_system.strip())

    summary = (session.get("summary") or "").strip()
    if mem.get("enabled", True) and summary:
        system_parts.append(
            "以下是本次对话更早部分的摘要，回答时请把它当作已知事实，"
            "不要重复询问其中已经确认过的信息：\n" + summary
        )

    ctx = []
    if system_parts:
        ctx.append({"role": "system", "content": "\n\n".join(system_parts)})
    for m in used:
        if m.get("role") in ("user", "assistant") and m.get("content"):
            ctx.append({"role": m["role"], "content": m["content"]})
    return ctx


def estimate_tokens(text):
    """粗略估算：中文≈1 字 1 token，英文≈4 字符 1 token。"""
    text = text or ""
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    other = len(text) - cjk
    return int(cjk + other / 3.5) + 1


def trim_to_budget(messages, max_tokens):
    """本地小模型上下文很短，从最早的非 system 消息开始丢，保住最新几轮。"""
    total = sum(estimate_tokens(m.get("content")) for m in messages)
    if total <= max_tokens:
        return messages
    head = [m for m in messages if m.get("role") == "system"]
    body = [m for m in messages if m.get("role") != "system"]
    while body and total > max_tokens:
        total -= estimate_tokens(body.pop(0).get("content"))
    return head + body


def needs_summary(session, cfg):
    mem = cfg.get("memory") or {}
    if not mem.get("enabled", True):
        return False
    msgs = session.get("messages") or []
    upto = int(session.get("summary_upto") or 0)
    pending = len(msgs) - upto
    trigger = max(2, int(mem.get("summary_trigger_turns", 10))) * 2
    return pending > trigger


def extractive_summary(messages, max_chars=700):
    """没有在线模型时的兜底：抽取用户说过的话 + 助手结论首句。"""
    lines = []
    for m in messages:
        text = (m.get("content") or "").strip().replace("\n", " ")
        if not text:
            continue
        if m.get("role") == "user":
            lines.append("· 用户说：" + text[:80])
        else:
            first = re.split(r"(?<=[。！？!?])", text)[0][:80]
            lines.append("· 助手答：" + first)
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = "……" + text[-max_chars:]
    return text


SUMMARY_PROMPT = """把下面这段聊天记录压缩成"长期记忆"，供后续对话使用。

要求：
- 只保留后续还会用到的东西：用户的身份/偏好/目标/约束、已确认的事实与结论、未完成的待办、约定的格式或风格。
- 丢掉寒暄、重复内容和无关细节。
- 用第三人称条目式陈述，不超过 200 字，不要加标题。

聊天记录：
{transcript}

只输出摘要正文。"""


def make_transcript(messages, max_chars=4000):
    out = []
    for m in messages:
        role = "用户" if m.get("role") == "user" else "助手"
        out.append(f"{role}：{(m.get('content') or '').strip()}")
    text = "\n".join(out)
    if len(text) > max_chars:
        text = text[: max_chars // 2] + "\n……\n" + text[-max_chars // 2:]
    return text


def update_summary(session, cfg, summarize_fn):
    """把窗口之外的旧消息并入摘要。summarize_fn(transcript)->str 可以为 None。"""
    msgs = session.get("messages") or []
    mem = cfg.get("memory") or {}
    window_turns = max(1, int(mem.get("window_turns", 6)))
    keep_from = max(0, len(msgs) - window_turns * 2)
    upto = int(session.get("summary_upto") or 0)
    if keep_from <= upto:
        return False, session.get("summary") or ""

    chunk = msgs[upto:keep_from]
    if not chunk:
        return False, session.get("summary") or ""

    old = (session.get("summary") or "").strip()
    transcript = make_transcript(chunk)
    if old:
        transcript = "已有的长期记忆摘要：\n" + old + "\n\n新增的聊天记录：\n" + transcript

    summary = None
    if summarize_fn:
        try:
            summary = (summarize_fn(transcript) or "").strip()
        except Exception:
            summary = None
    if not summary:
        summary = extractive_summary(chunk, int(mem.get("summary_max_chars", 700)))

    limit = int(mem.get("summary_max_chars", 700))
    if len(summary) > limit:
        summary = summary[:limit] + "……"
    session["summary"] = summary
    session["summary_upto"] = keep_from
    return True, summary
