"""在线引擎：任何 OpenAI 兼容的 /chat/completions（DeepSeek、Moonshot、硅基流动、Ollama 等）。"""
import json
import urllib.error
import urllib.request


class EngineError(RuntimeError):
    pass


def _endpoint(cfg):
    base = (cfg.get("base_url") or "").rstrip("/")
    if not base:
        raise EngineError("还没有配置接口地址（Base URL）")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def available(cfg):
    if not (cfg.get("api_key") or "").strip():
        return False, "未配置 API Key"
    if not (cfg.get("base_url") or "").strip():
        return False, "未配置 Base URL"
    return True, ""


def _http_detail(e):
    """把服务端返回的 body 摘要出来，比只报状态码有用得多。"""
    try:
        raw = e.read().decode("utf-8", "replace").strip()
    except Exception:
        return str(getattr(e, "reason", "") or e)
    if not raw:
        return str(getattr(e, "reason", "") or e)
    try:
        obj = json.loads(raw)
        err = obj.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("type") or ""
        elif isinstance(err, str):
            msg = err
        else:
            msg = obj.get("message") or obj.get("detail") or ""
        if msg:
            return str(msg)[:200]
    except ValueError:
        pass
    return raw[:200].replace("\n", " ")


def _post(url, body, key, timeout):
    """发请求；某些模型（如 Kimi K2）只允许 temperature=1，遇到 400 自动按 1 重试一次。"""
    def fire(payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + key,
                "Accept": "text/event-stream",
            },
            method="POST",
        )
        return urllib.request.urlopen(req, timeout=timeout)

    try:
        return fire(body)
    except urllib.error.HTTPError as e:
        detail = _http_detail(e)
        if e.code == 400 and "temperature" in detail.lower():
            body = dict(body)
            body["temperature"] = 1.0
            try:
                return fire(body)
            except urllib.error.HTTPError as e2:
                raise EngineError(f"接口返回 {e2.code}：" + _http_detail(e2))
            except urllib.error.URLError as e2:
                raise EngineError(f"连不上接口（{e2.reason}）")
        hint = ""
        if e.code == 401:
            hint = "（Key 无效）"
        elif e.code == 402:
            hint = "（余额不足）"
        elif e.code == 404:
            hint = "（模型名或 Base URL 不对）"
        elif e.code == 429:
            hint = "（限流或额度用完）"
        raise EngineError(f"接口返回 {e.code}：{detail}{hint}")
    except urllib.error.URLError as e:
        raise EngineError(f"连不上接口（{e.reason}），检查 Base URL / 网络 / 代理")


def _body(messages, cfg, stream):
    return {
        "model": cfg.get("model") or "deepseek-chat",
        "messages": messages,
        "temperature": float(cfg.get("temperature", 0.7)),
        "max_tokens": int(cfg.get("max_tokens", 1200)),
        "stream": stream,
    }


def stream(messages, cfg, stop=None):
    key = (cfg.get("api_key") or "").strip()
    ok, reason = available(cfg)
    if not ok:
        raise EngineError(reason + "，点右上角「设置」填写")
    if not (cfg.get("model") or "").strip():
        raise EngineError("还没填模型名（model）")

    resp = _post(_endpoint(cfg), _body(messages, cfg, True), key, float(cfg.get("timeout", 120)))
    with resp:
        for raw in resp:
            if stop and stop():
                return
            line = raw.decode("utf-8", "replace").strip()
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                return
            try:
                obj = json.loads(data)
            except ValueError:
                continue
            if obj.get("error"):
                err = obj["error"]
                raise EngineError(str(err.get("message") if isinstance(err, dict) else err))
            for ch in obj.get("choices") or []:
                piece = (ch.get("delta") or {}).get("content")
                if piece:
                    yield piece


def complete(messages, cfg, max_tokens=400, temperature=None):
    """非流式一次性调用（生成摘要、测试连接用）。"""
    key = (cfg.get("api_key") or "").strip()
    ok, reason = available(cfg)
    if not ok:
        raise EngineError(reason)
    body = _body(messages, cfg, False)
    body["max_tokens"] = max_tokens
    if temperature is not None:
        body["temperature"] = temperature
    resp = _post(_endpoint(cfg), body, key, float(cfg.get("timeout", 120)))
    with resp:
        obj = json.loads(resp.read().decode("utf-8", "replace"))
    return ((obj.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
