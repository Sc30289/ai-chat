"""本地引擎：直接跑 minimind-vscode 里训练好的 64M 小模型（贪心/采样 + 流式）。

依赖 minimind-vscode/.venv 里的 torch / transformers，所以用那个解释器启动本服务即可。
"""
import os
import queue
import re
import sys
import threading

_STATE = {"model": None, "tokenizer": None, "weight": None, "lora": None, "dir": None, "device": None, "error": None}
_LOCK = threading.Lock()

SPECIAL = re.compile(r"<\|im_(?:start|end)\|>|<\|endoftext\|>")


class _IdQueue:
    """收集生成的 token id（第一次 put 是 prompt，跳过）。

    不用 transformers 的 TextIteratorStreamer：它按"每字符"切片，中文多字节
    字符会被切成半个，前端就会看到「」这种乱码。这里改成累积 id 再整体解码。
    """

    def __init__(self):
        self.q = queue.Queue()
        self._first = True

    def put(self, value):
        ids = value.tolist() if hasattr(value, "tolist") else value
        if isinstance(ids, list) and ids and isinstance(ids[0], list):
            ids = ids[0]
        if not isinstance(ids, list):
            ids = [ids]
        if self._first:
            self._first = False
            return
        self.q.put(ids)

    def end(self):
        self.q.put(None)


def _delta(prev, cur):
    """cur 相对 prev 的新增部分（能对齐就只发增量）。"""
    if cur.startswith(prev):
        return cur[len(prev):]
    n = 0
    while n < min(len(prev), len(cur)) and prev[n] == cur[n]:
        n += 1
    return cur[n:]


class EngineError(RuntimeError):
    pass


def _add_path(minimind_dir):
    d = os.path.abspath(minimind_dir)
    if d not in sys.path:
        sys.path.insert(0, d)
    return d


def _weight_path(d, weight, hidden=768):
    return os.path.join(d, "out", f"{weight}_{hidden}.pth")


def available(cfg):
    d = os.path.abspath(cfg.get("minimind_dir") or "")
    if not os.path.isdir(d):
        return False, f"找不到 minimind 目录：{d}"
    if not os.path.exists(_weight_path(d, cfg.get("weight") or "full_sft")):
        return False, "找不到权重文件 out/full_sft_768.pth"
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
    except ImportError:
        return False, "当前解释器没有 torch/transformers，请用 minimind-vscode/.venv 的 python 启动"
    return True, ""


def status():
    with _LOCK:
        return {
            "loaded": _STATE["model"] is not None,
            "weight": _STATE["weight"],
            "lora": _STATE["lora"],
            "device": _STATE["device"],
            "error": _STATE["error"],
        }


def list_weights(cfg):
    """扫描 out/*_768.pth，给前端做模型下拉框。最新的排在前面。"""
    d = os.path.abspath(cfg.get("minimind_dir") or "")
    out_dir = os.path.join(d, "out")
    items = []
    if not os.path.isdir(out_dir):
        return items
    for name in os.listdir(out_dir):
        if not name.endswith("_768.pth") or name.startswith("lora_"):
            continue
        full = os.path.join(out_dir, name)
        try:
            st = os.stat(full)
        except OSError:
            continue
        items.append({"name": name[:-8], "size_mb": round(st.st_size / 1e6, 1), "mtime": st.st_mtime})
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


def _device_of():
    import torch

    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def _zero_lora(model):
    for _, module in model.named_modules():
        lora = getattr(module, "lora", None)
        if lora is not None:
            lora.B.weight.data.zero_()


def _load(cfg, lora_name):
    import torch
    from transformers import AutoTokenizer

    d = _add_path(cfg.get("minimind_dir"))
    weight = cfg.get("weight") or "full_sft"
    ckp = _weight_path(d, weight)
    if not os.path.exists(ckp):
        raise EngineError(f"权重不存在：{ckp}")

    from model.model_my import GPTForCausalLM, TransformerConfig

    device = _device_of()
    tokenizer = AutoTokenizer.from_pretrained(os.path.join(d, "modelT"))
    config = TransformerConfig(hidden_size=768, num_hidden_layers=8)
    model = GPTForCausalLM(config)
    state = torch.load(ckp, map_location=device)
    model.load_state_dict(state, strict=False)
    dtype = torch.bfloat16 if device == "cuda" else torch.float32
    model = model.to(device, dtype=dtype).eval()

    from model.model_lora import apply_lora, load_lora

    apply_lora(model)
    _STATE.update(model=model, tokenizer=tokenizer, dir=d, device=device,
                  error=None, lora=None, weight=weight)
    _set_lora(lora_name)
    if device == "cuda":
        torch.cuda.empty_cache()
    return model, tokenizer


def _set_lora(lora_name):
    model = _STATE["model"]
    if model is None:
        return
    from model.model_lora import load_lora

    name = (lora_name or "none").strip() or "none"
    if name.lower() in ("none", "", "null"):
        _zero_lora(model)
        _STATE["lora"] = "none"
        return
    path = os.path.join(_STATE["dir"], "out", f"{name}_768.pth")
    if not os.path.exists(path):
        raise EngineError(f"LoRA 权重不存在：{path}")
    _zero_lora(model)
    load_lora(model, path)
    _STATE["lora"] = name


def list_loras(cfg):
    """扫描 out/lora_*_768.pth。"""
    d = os.path.abspath(cfg.get("minimind_dir") or "")
    out_dir = os.path.join(d, "out")
    items = []
    if not os.path.isdir(out_dir):
        return items
    for name in os.listdir(out_dir):
        if name.startswith("lora_") and name.endswith("_768.pth"):
            items.append(name[:-8])
    items.sort()
    return items


def stream(messages, cfg, stop=None):
    """messages: [{"role","content"}]，含 system。产出一段段文本。"""
    lora_name = cfg.get("lora") or "none"
    weight_name = cfg.get("weight") or "full_sft"
    with _LOCK:
        if _STATE["model"] is None or _STATE["weight"] != weight_name:
            yield "\0LOADING"  # 由上层换成"正在加载模型"提示
            _load(cfg, lora_name)
        elif _STATE["lora"] != lora_name:
            _set_lora(lora_name)

        model = _STATE["model"]
        tokenizer = _STATE["tokenizer"]

    from dataset.lm_dataset import IM_END, render_chat

    ids = [m for m in messages if m.get("role") != "system"]
    system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system")
    chat = []
    if system:
        chat.append({"role": "system", "content": system})
    chat.extend({"role": m["role"], "content": m.get("content") or ""} for m in ids)

    text = render_chat(chat, add_generation_prompt=True)
    enc = tokenizer(text, return_tensors="pt", add_special_tokens=False)
    enc = {k: v.to(_STATE["device"]) for k, v in enc.items()}

    streamer = _IdQueue()
    kwargs = dict(
        inputs=enc["input_ids"],
        attention_mask=enc.get("attention_mask"),
        max_new_tokens=int(cfg.get("max_new_tokens", 96)),
        temperature=float(cfg.get("temperature", 0.85)),
        top_p=float(cfg.get("top_p", 0.9)),
        repetition_penalty=float(cfg.get("repetition_penalty", 1.1)),
        # 64M 小模型采样很容易跑偏，默认贪心（temperature 不起作用）
        do_sample=not bool(cfg.get("greedy", True)),
        eos_token_id=tokenizer.eos_token_id,
        streamer=streamer,
    )
    thread = threading.Thread(target=model.generate, kwargs=kwargs, daemon=True)
    thread.start()

    gen_ids = []
    emitted = ""
    try:
        while True:
            item = streamer.q.get()
            if item is None:
                break
            if stop and stop():
                return
            gen_ids.extend(item)
            raw = tokenizer.decode(gen_ids, skip_special_tokens=False)
            ended = IM_END in raw
            if ended:
                raw = raw.split(IM_END)[0]
            cur = SPECIAL.sub("", raw)
            # 一个汉字可能被拆在两个 token 里，前半截解码出来是 U+FFFD。
            # 尾部先扣住不发，等下一个 token 把它补全再发。
            safe = cur
            while safe.endswith("\ufffd"):
                safe = safe[:-1]
            if safe != emitted:
                piece = _delta(emitted, safe)
                if piece:
                    yield piece
                emitted = safe
            if ended:
                return
    finally:
        thread.join(timeout=0.2)


def warmup(cfg):
    with _LOCK:
        if _STATE["model"] is None:
            _load(cfg, cfg.get("lora") or "none")
    return status()
