"""配置读写：data/config.json（远端接口列表 + 本地模型 + 记忆设置）。

配置结构（v2）：
    engine        当前引擎："local" 或某个远端的 id
    remotes       [ {id,name,base_url,api_key,api_key_env,model,temperature,max_tokens,timeout}, ... ]
    local         本地 miniMind 配置
    memory        记忆设置
    system_prompt 通用 system（在线模型用）

旧的 v1 配置（单个 "online" 块）会在读取时自动迁移成 remotes[0]，密钥不丢。
"""
import copy
import json
import os

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")

# 新增远端时可以从这些预设里挑
REMOTE_PRESETS = [
    {"id": "deepseek", "name": "DeepSeek", "base_url": "https://api.deepseek.com",
     "model": "deepseek-chat", "api_key_env": "DEEPSEEK_API_KEY"},
    {"id": "moonshot", "name": "Moonshot 月之暗面", "base_url": "https://api.moonshot.cn/v1",
     "model": "kimi-k2.6", "api_key_env": "MOONSHOT_API_KEY", "temperature": 1.0},
    {"id": "siliconflow", "name": "硅基流动", "base_url": "https://api.siliconflow.cn/v1",
     "model": "deepseek-ai/DeepSeek-V3", "api_key_env": "SILICONFLOW_API_KEY"},
    {"id": "ollama", "name": "本地 Ollama", "base_url": "http://127.0.0.1:11434/v1",
     "model": "qwen2.5:7b", "api_key_env": ""},
    {"id": "custom", "name": "自定义", "base_url": "", "model": "", "api_key_env": ""},
]

REMOTE_FIELDS = ("id", "name", "base_url", "api_key", "api_key_env", "model",
                 "temperature", "max_tokens", "timeout")


def default_remote(preset_id="deepseek"):
    p = next((x for x in REMOTE_PRESETS if x["id"] == preset_id), REMOTE_PRESETS[0])
    return {
        "id": p["id"],
        "name": p["name"],
        "base_url": p["base_url"],
        "model": p["model"],
        "api_key_env": p.get("api_key_env", ""),
        "api_key": "",
        # K2 系列（Kimi）只接受 temperature=1，预设里就带上
        "temperature": float(p.get("temperature", 0.7)),
        "max_tokens": 1200,
        "timeout": 120,
    }


DEFAULTS = {
    "engine": "local",
    "system_prompt": (
        "你是一个中文 AI 助手。回答要准确、简洁，先给结论再补充细节；"
        "如果用户之前说过相关的偏好、身份或约束，要主动沿用。"
    ),
    "remotes": [default_remote("deepseek")],
    "local": {
        "minimind_dir": os.path.abspath(os.path.join(ROOT, "..", "minimind-vscode")),
        "weight": "full_sft",
        "lora": "none",
        # 64M 小模型很吃 system 的形式：训练时用的是这一句，推理就别换措辞
        "system_prompt": "你是一个中文 AI 助手。",
        # 采样容易跑偏，默认贪心；想要多样性就设成 false 再调 temperature
        "greedy": True,
        "temperature": 0.85,
        "top_p": 0.9,
        "repetition_penalty": 1.1,
        "max_new_tokens": 96,
        "max_prompt_tokens": 320,
    },
    "memory": {
        "enabled": True,
        "window_turns": 6,
        "summary_trigger_turns": 10,
        "summary_max_chars": 700,
    },
}


def _deep_merge(base, override):
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(os.path.join(DATA_DIR, "sessions"), exist_ok=True)


def _migrate_v1(raw):
    """把旧结构 {"online": {...}} 迁移成 remotes 列表。返回 (raw, changed)。"""
    if "remotes" in raw or "online" not in raw:
        return raw, False
    online = raw.pop("online") or {}
    r = default_remote("deepseek")
    for k in REMOTE_FIELDS:
        if k in online and online[k] not in (None, ""):
            r[k] = online[k]
    raw["remotes"] = [r]
    if raw.get("engine") in (None, "", "online"):
        raw["engine"] = r["id"]
    return raw, True


def _apply_env_keys(cfg):
    for r in cfg.get("remotes") or []:
        if not (r.get("api_key") or "").strip() and r.get("api_key_env"):
            r["api_key"] = os.environ.get(r["api_key_env"], "")
    return cfg


def load():
    ensure_dirs()
    raw = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f) or {}
        except (OSError, ValueError):
            raw = {}

    raw, migrated = _migrate_v1(raw)
    cfg = _deep_merge(DEFAULTS, raw)
    cfg = _apply_env_keys(cfg)

    ids = [r.get("id") for r in cfg.get("remotes") or []]
    if cfg.get("engine") != "local" and cfg.get("engine") not in ids:
        cfg["engine"] = "local"
    if not cfg.get("remotes"):
        cfg["remotes"] = [default_remote("deepseek")]
        cfg["engine"] = "local"

    if migrated:
        save(cfg)
    return cfg


def save(cfg):
    ensure_dirs()
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def find_remote(cfg, rid):
    for r in cfg.get("remotes") or []:
        if r.get("id") == rid:
            return r
    return None


def _prepare_remotes(new_list, old_list):
    """前端提交的远端列表：空 Key 表示"不修改"，从旧配置里按 id 找回。"""
    old = {r.get("id"): r for r in (old_list or [])}
    out = []
    used = set()
    for i, r in enumerate(new_list or []):
        rid = (r.get("id") or "").strip() or f"remote{i + 1}"
        base_id, n = rid, 1
        while rid in used:
            n += 1
            rid = f"{base_id}{n}"
        used.add(rid)

        item = default_remote("custom")
        item.update({k: v for k, v in (r or {}).items() if k in REMOTE_FIELDS})
        item["id"] = rid
        item["name"] = (item.get("name") or rid)[:40]
        prev = old.get(rid) or {}
        if not (item.get("api_key") or "").strip():
            item["api_key"] = prev.get("api_key", "")
        if not item.get("api_key_env"):
            item["api_key_env"] = prev.get("api_key_env", "")
        try:
            item["temperature"] = float(item.get("temperature") or 0.7)
        except (TypeError, ValueError):
            item["temperature"] = 0.7
        try:
            item["max_tokens"] = int(item.get("max_tokens") or 1200)
        except (TypeError, ValueError):
            item["max_tokens"] = 1200
        out.append(item)
    return out


def merge_patch(patch):
    """把前端提交的局部配置合并进去并落盘。"""
    cfg = load()
    patch = dict(patch or {})
    if "remotes" in patch:
        patch["remotes"] = _prepare_remotes(patch["remotes"], cfg.get("remotes"))
    merged = _deep_merge(cfg, patch)

    ids = [r.get("id") for r in merged.get("remotes") or []]
    if merged.get("engine") != "local" and merged.get("engine") not in ids:
        merged["engine"] = ids[0] if ids else "local"
    save(merged)
    return merged


def public(cfg):
    """给前端看的配置（密钥只报是否存在）。"""
    out = copy.deepcopy(cfg)
    remotes = []
    for r in out.get("remotes") or []:
        key = (r.get("api_key") or "").strip()
        remotes.append({
            "id": r["id"],
            "name": r.get("name") or r["id"],
            "base_url": r.get("base_url") or "",
            "model": r.get("model") or "",
            "api_key": "",
            "api_key_env": r.get("api_key_env") or "",
            "temperature": r.get("temperature", 0.7),
            "max_tokens": r.get("max_tokens", 1200),
            "has_api_key": bool(key),
            "api_key_hint": (key[:5] + "…" + key[-4:]) if len(key) >= 12 else ("已设置" if key else ""),
        })
    out["remotes"] = remotes
    out["presets"] = REMOTE_PRESETS
    return out
