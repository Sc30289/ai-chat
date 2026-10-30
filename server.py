"""AI 对话服务：静态页面 + JSON 接口 + SSE 流式输出。

启动（任何 python 都行，见 maybe_reexec）：
    python server.py                # 默认 8765，自动开浏览器
    python server.py --port 8900 --no-open
"""
import argparse
import json
import mimetypes
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config
import auth
import memory
import store
from engines import local, online

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(ROOT, "web")

STOPS = {}
STOP_LOCK = threading.Lock()


def stop_flag(sid):
    with STOP_LOCK:
        STOPS[sid] = False
        return lambda: STOPS.get(sid, False)


def request_stop(sid):
    with STOP_LOCK:
        STOPS[sid] = True


class SSE:
    def __init__(self, handler):
        self.h = handler
        self.dead = False

    def send(self, event, data):
        if self.dead:
            return
        try:
            payload = json.dumps(data, ensure_ascii=False)
            self.h.wfile.write(f"event: {event}\ndata: {payload}\n\n".encode("utf-8"))
            self.h.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.dead = True


def engine_stream(messages, engine_name, cfg, stop):
    if engine_name == "local":
        lcfg = dict(cfg["local"])
        # 64M 小模型的 context 很短，也学不会"读 system 里的偏好"：
        # 这里用训练时那句短 system 覆盖掉通用的长 system，并丢掉摘要注入。
        short_sys = (lcfg.get("system_prompt") or "").strip()
        msgs = [m for m in messages if m.get("role") != "system"]
        if short_sys:
            msgs.insert(0, {"role": "system", "content": short_sys})
        msgs = [dict(m) for m in msgs]
        for m in msgs:
            if m.get("role") == "system" and len(m.get("content") or "") > 200:
                m["content"] = m["content"][:200] + "……"
        msgs = memory.trim_to_budget(msgs, int(lcfg.get("max_prompt_tokens", 320)))
        for piece in local.stream(msgs, lcfg, stop=stop):
            if piece == "\0LOADING":
                yield ("status", "首次加载本地模型，约 5~15 秒…")
            else:
                yield ("delta", piece)
    else:
        remote = config.find_remote(cfg, engine_name)
        if not remote:
            raise RuntimeError(f"远端「{engine_name}」不存在，去设置里加一个")
        for piece in online.stream(messages, remote, stop=stop):
            yield ("delta", piece)


def engine_available(name, cfg):
    if name == "local":
        return local.available(cfg["local"])
    remote = config.find_remote(cfg, name)
    if not remote:
        return False, "远端已删除"
    return online.available(remote)


def summarize_fn(cfg):
    """挑一个有 key 的远端来写摘要；没有就返回 None（退化成抽取式）。"""
    active = config.find_remote(cfg, cfg.get("engine"))
    candidates = [active] if active else []
    candidates += [r for r in (cfg.get("remotes") or []) if r is not active]
    for r in candidates:
        if (r.get("api_key") or "").strip():
            return lambda tr: online.complete(
                [{"role": "user", "content": memory.SUMMARY_PROMPT.format(transcript=tr)}], r, 400)
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "ai-chat/1.0"
    protocol_version = "HTTP/1.1"

    # ---------- 基础工具 ----------
    def log_message(self, fmt, *args):
        # 终端被关掉后 stderr 是断的，这里绝不能抛异常（否则每个请求都会挂）
        try:
            sys.stderr.write("%s  %s\n" % (time.strftime("%H:%M:%S"), fmt % args))
        except (OSError, ValueError):
            pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _json_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            # utf-8-sig：容忍某些客户端带 BOM
            return json.loads(self.rfile.read(n).decode("utf-8-sig") or "{}")
        except ValueError:
            return {}

    def _error(self, code, msg):
        self._send(code, {"ok": False, "error": msg})

    # ---------- 登录态 ----------
    def _token(self):
        # 优先用请求头里的令牌（前端存 localStorage），兼容旧 Cookie
        header = (self.headers.get("X-Auth-Token") or "").strip()
        if header:
            return header
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == auth.COOKIE_NAME:
                return value.strip()
        return ""

    def _user(self):
        # 不要缓存：keep-alive 会复用同一个 Handler 实例，
        # 缓存会导致"退出登录后旧连接仍能访问"。
        return auth.user_by_token(self._token())

    def _need_user(self):
        u = self._user()
        if not u:
            self._error(401, "未登录或登录已过期，请重新登录")
            return None
        return u

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    # ---------- 路由 ----------
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            # 旧地址（书签/历史记录里还留着独立登录页）统一回到首页
            if path in ("/login.html", "/login", "/signin", "/index.html"):
                return self._redirect("/")
            if path.startswith("/api/"):
                if path == "/api/me":
                    u = self._user()
                    if not u:
                        return self._send(200, {"ok": True, "user": None})
                    return self._send(200, {"ok": True, "user": public_user(u)})
                if not self._need_user():
                    return
                return self.api_get(path)
            return self.static(path)
        except Exception:
            traceback.print_exc()
            self._error(500, "服务器内部错误")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path in ("/api/login", "/api/register"):
                return self.api_auth(path)
            if path == "/api/logout":
                return self.api_logout()
            if path == "/api/chat":
                if not self._need_user():
                    return
                return self.api_chat()
            if path.startswith("/api/"):
                if not self._need_user():
                    return
                return self.api_post(path)
            self._error(404, "not found")
        except Exception:
            traceback.print_exc()
            try:
                self._error(500, "服务器内部错误")
            except Exception:
                pass

    def do_DELETE(self):
        path = urllib.parse.urlparse(self.path).path
        parts = path.strip("/").split("/")
        if not self._need_user():
            return
        if len(parts) == 3 and parts[:2] == ["api", "sessions"]:
            ok = store.delete(parts[2], owner=self._user()["id"])
            return self._send(200, {"ok": ok})
        self._error(404, "not found")

    def do_PATCH(self):
        path = urllib.parse.urlparse(self.path).path
        parts = path.strip("/").split("/")
        body = self._json_body()
        if not self._need_user():
            return
        if len(parts) == 3 and parts[:2] == ["api", "sessions"]:
            s = store.load(parts[2], owner=self._user()["id"])
            if not s:
                return self._error(404, "会话不存在")
            if "title" in body:
                s["title"] = str(body["title"])[:60] or "未命名"
            if "summary" in body:
                s["summary"] = str(body["summary"])
            store.save(s)
            return self._send(200, {"ok": True, "session": public_session(s)})
        self._error(404, "not found")

    # ---------- 静态文件 ----------
    def static(self, path):
        if path in ("/", ""):
            path = "/index.html"
        rel = urllib.parse.unquote(path.lstrip("/"))
        full = os.path.abspath(os.path.join(WEB_DIR, rel))
        if not full.startswith(WEB_DIR) or not os.path.isfile(full):
            return self._send(404, "文件不存在", "text/plain; charset=utf-8")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as f:
            raw = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(raw)

    # ---------- API ----------
    def api_get(self, path):
        cfg = config.load()
        uid = self._user()["id"]
        if path == "/api/state":
            return self._send(200, self.state_payload(cfg))
        if path == "/api/sessions":
            return self._send(200, {"ok": True, "sessions": store.list_sessions(owner=uid)})
        if path.startswith("/api/sessions/"):
            sid = path.strip("/").split("/")[2]
            s = store.load(sid, owner=uid)
            if not s:
                return self._error(404, "会话不存在")
            return self._send(200, {"ok": True, "session": public_session(s)})
        self._error(404, "not found")

    def state_payload(self, cfg):
        engines = {}
        for name in ("local",) + tuple(r["id"] for r in cfg.get("remotes") or []):
            ok, why = engine_available(name, cfg)
            engines[name] = {"available": ok, "reason": why}
        engines["local"]["status"] = local.status()
        uid = self._user()["id"] if self._user() else None
        return {
            "ok": True,
            "me": public_user(self._user()),
            "config": config.public(cfg),
            "engines": engines,
            "remotes": config.public(cfg)["remotes"],
            "local_weights": local.list_weights(cfg["local"]),
            "local_loras": local.list_loras(cfg["local"]),
            "sessions": store.list_sessions(owner=uid),
        }

    def api_post(self, path):
        cfg = config.load()
        body = self._json_body()

        if path == "/api/test_online":
            # 允许用界面上还没保存的值直接测，省得"保存了才发现 key 不对"
            rid = str(body.get("remote_id") or "").strip()
            remote = config.find_remote(cfg, rid) if rid else None
            if remote is None:
                remote = config.find_remote(cfg, cfg.get("engine"))
            oc = dict(remote or (cfg.get("remotes") or [config.default_remote()])[0])
            if body.get("base_url"):
                oc["base_url"] = str(body["base_url"]).strip()
            if body.get("model"):
                oc["model"] = str(body["model"]).strip()
            if (body.get("api_key") or "").strip():
                oc["api_key"] = str(body["api_key"]).strip()
            try:
                reply = online.complete(
                    [{"role": "user", "content": "只回复两个字：收到"}], oc, max_tokens=8)
                return self._send(200, {"ok": True, "connected": True,
                                        "message": "连接成功，模型回复：" + ((reply or "").strip()[:30] or "（空）")})
            except Exception as e:
                msg = str(e)
                hint = ""
                if "401" in msg:
                    hint = "　→ Key 无效或已被删除，检查是否复制完整、前后没有空格"
                elif "402" in msg:
                    hint = "　→ 账户余额不足，需要先去充值"
                elif "404" in msg:
                    hint = "　→ 模型名或 Base URL 不对（DeepSeek 用 https://api.deepseek.com + deepseek-chat）"
                elif "429" in msg:
                    hint = "　→ 请求太频繁或超出额度，稍等再试"
                elif "连不上" in msg or "URLError" in msg:
                    hint = "　→ 网络或代理问题，或该地址本机访问不到"
                return self._send(200, {"ok": True, "connected": False, "message": msg + hint})

        if path == "/api/config":
            patch = {k: v for k, v in body.items()
                     if k in ("engine", "system_prompt", "remotes", "local", "memory")}
            cfg = config.merge_patch(patch)
            return self._send(200, self.state_payload(cfg))

        if path == "/api/sessions":
            s = store.new_session(owner=self._user()["id"])
            return self._send(200, {"ok": True, "session": public_session(s)})

        if path == "/api/stop":
            request_stop(str(body.get("session_id") or ""))
            return self._send(200, {"ok": True})

        if path == "/api/profile":
            u = self._user()
            try:
                nu = auth.rename_user(u, str(body.get("username") or ""))
            except auth.AuthError as e:
                return self._error(400, str(e))
            return self._send(200, {"ok": True, "user": public_user(nu), "message": "用户名已修改"})

        if path == "/api/password":
            u = auth.find_by_id(self._user()["id"])
            if not auth.check_password(u, str(body.get("old_password") or "")):
                return self._error(400, "原密码不对")
            try:
                auth.set_password(u, str(body.get("new_password") or ""))
            except auth.AuthError as e:
                return self._error(400, str(e))
            # 改完密码把其它设备的登录态踢掉，当前这个保留
            auth.drop_user_tokens(u["id"], keep=self._token())
            return self._send(200, {"ok": True, "message": "密码已修改，其它设备需要重新登录"})

        if path == "/api/warmup":
            try:
                local.warmup(cfg["local"])
                return self._send(200, {"ok": True, "status": local.status()})
            except Exception as e:
                return self._error(500, str(e))

        if path.endswith("/summarize"):
            parts = path.strip("/").split("/")
            sid = parts[2]
            s = store.load(sid, owner=self._user()["id"])
            if not s:
                return self._error(404, "会话不存在")
            fn = summarize_fn(cfg)
            changed, summary = memory.update_summary(s, cfg, fn)
            if not changed:
                summary = s.get("summary") or ""
            store.save(s)
            return self._send(200, {"ok": True, "summary": summary, "changed": changed})

        self._error(404, "not found")

    def api_auth(self, path):
        body = self._json_body()
        username = str(body.get("username") or "").strip()
        password = str(body.get("password") or "")
        try:
            if path == "/api/register":
                if body.get("confirm") is not None and str(body.get("confirm")) != password:
                    return self._error(400, "两次输入的密码不一致")
                user = auth.create_user(username, password)
                first = auth.count_users() == 1
                if first:
                    # 第一个账号顺手接管之前没有归属的历史会话，并给个示例
                    store.claim_ownerless(user["id"])
                    ensure_seed_for(user["id"])
            else:
                user = auth.find_by_name(username)
                if not auth.check_password(user, password):
                    return self._error(400, "用户名或密码不对")
            token = auth.create_token(user["id"])
            return self._send(200, {"ok": True, "user": public_user(user), "token": token})
        except auth.AuthError as e:
            return self._error(400, str(e))

    def api_logout(self):
        auth.drop_token(self._token())
        return self._send(200, {"ok": True})

    def api_chat(self):
        body = self._json_body()
        cfg = config.load()
        sid = str(body.get("session_id") or "")
        content = (body.get("content") or "").strip()
        regenerate = bool(body.get("regenerate"))
        engine_name = body.get("engine") or cfg.get("engine") or "online"

        uid = self._user()["id"]
        session = store.load(sid, owner=uid) if sid else None
        if session is None:
            session = store.new_session(owner=uid)
            sid = session["id"]

        if regenerate:
            store.truncate_last_assistant(session)
        elif not content:
            return self._error(400, "内容为空")
        else:
            store.append_message(session, "user", content)
        store.save(session)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        sse = SSE(self)
        sse.send("meta", {"session_id": sid, "title": session.get("title"), "engine": engine_name})

        stop = stop_flag(sid)
        ctx = memory.build_context(session, cfg)
        reply = []
        err = None
        t0 = time.time()
        try:
            for kind, payload in engine_stream(ctx, engine_name, cfg, stop):
                if kind == "status":
                    sse.send("status", {"text": payload})
                else:
                    reply.append(payload)
                    sse.send("delta", {"text": payload})
        except Exception as e:
            err = str(e) or e.__class__.__name__
            sse.send("error", {"message": err})

        text = "".join(reply).strip()
        if text:
            store.append_message(session, "assistant", text, {"engine": engine_name})
            store.save(session)
        sse.send("done", {
            "session_id": sid,
            "text": text,
            "elapsed": round(time.time() - t0, 2),
            "stopped": bool(stop()),
            "error": err,
            "title": session.get("title"),
        })

        # 对话变长后滚动更新长期记忆
        try:
            if text and memory.needs_summary(session, cfg):
                fn = summarize_fn(cfg)
                changed, summary = memory.update_summary(session, cfg, fn)
                if changed:
                    store.save(session)
                    sse.send("memory", {"summary": summary, "covered": session.get("summary_upto")})
        except Exception:
            traceback.print_exc()

        self.close_connection = True


def public_session(s):
    return {
        "id": s["id"],
        "title": s.get("title"),
        "created": s.get("created"),
        "updated": s.get("updated"),
        "summary": s.get("summary") or "",
        "summary_upto": s.get("summary_upto") or 0,
        "messages": [{"role": m["role"], "content": m.get("content") or ""} for m in s.get("messages") or []],
    }


def public_user(u):
    if not u:
        return None
    return {"id": u["id"], "username": u["username"], "created": u.get("created")}


def ensure_seed_for(owner):
    """给某个账号塞一个示例会话（仅当他还没有任何会话时）。"""
    if store.list_sessions(owner=owner):
        return
    s = store.new_session("示例：让 AI 记住你", owner=owner)
    store.append_message(s, "user", "我叫小林，做前端开发，最近在学 PyTorch。")
    store.append_message(s, "assistant", "你好小林！前端 + PyTorch 是个不错的组合，需要我帮你做个学习路线吗？", {"engine": "seed"})
    store.append_message(s, "user", "先不用。你记一下：以后回答我的问题，代码示例都用 TypeScript。")
    store.append_message(s, "assistant", "记下了：后续代码示例默认用 TypeScript。", {"engine": "seed"})
    store.save(s)


def _venv_python():
    """minimind 的 venv 解释器（装了 torch，本地模型要用它）。"""
    p = os.path.abspath(os.path.join(ROOT, "..", "minimind-vscode", ".venv", "Scripts", "python.exe"))
    return p if os.path.exists(p) else None


def _pid_alive(pid):
    """判断进程是否还活着（Windows 上 os.kill(pid, 0) 不可靠）。"""
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
            return bool(ok) and code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _start_parent_watchdog():
    """父进程（启动器/终端）没了就自己退出，避免残留进程占着端口不放。"""
    ppid = os.environ.get("AI_CHAT_PARENT_PID")
    if not ppid:
        return
    try:
        ppid = int(ppid)
    except ValueError:
        return

    def watch():
        while True:
            time.sleep(5)
            if not _pid_alive(ppid):
                os._exit(0)

    threading.Thread(target=watch, daemon=True).start()


def maybe_reexec():
    """保证「随便哪个 python server.py」都能跑起本地模型。

    如果当前解释器没有 torch、而 minimind 的 venv 存在，就用那个解释器重启自己。
    已重入（环境变量 AI_CHAT_REEXEC=1）或本来就是 venv python 时直接返回。
    """
    if os.environ.get("AI_CHAT_REEXEC") == "1":
        return
    venv = _venv_python()
    if not venv:
        return
    if os.path.abspath(sys.executable).lower() == venv.lower():
        return
    try:
        import torch  # noqa: F401
        return  # 当前解释器自己就有 torch，不折腾
    except ImportError:
        pass
    print("[launcher] this python has no torch, switching to:", venv, flush=True)
    env = dict(os.environ, AI_CHAT_REEXEC="1", PYTHONIOENCODING="utf-8",
               AI_CHAT_PARENT_PID=str(os.getpid()))
    cmd = [venv, os.path.abspath(__file__)] + sys.argv[1:]
    sys.exit(subprocess.call(cmd, env=env))


class ChatServer(ThreadingHTTPServer):
    daemon_threads = True
    # Windows 上 SO_REUSEADDR 允许两个进程绑同一端口（第二个会静默抢走连接），
    # 关掉它，端口被占用时直接报错而不是"能连上但没响应"
    allow_reuse_address = os.name != "nt"

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        # 浏览器关掉连接/中途取消时会抛这些，不算错误，别刷屏
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def main():
    maybe_reexec()
    _start_parent_watchdog()

    ap = argparse.ArgumentParser(description="AI 对话网页服务")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    # 控制台/管道下都立刻输出，避免启动横幅被缓冲住
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    config.ensure_dirs()

    first_run = not os.path.exists(config.CONFIG_PATH)
    cfg = config.load()

    # 云端部署：没有本地权重时自动切到第一个在线引擎，避免默认引擎不可用
    try:
        ok_local, _ = local.available(cfg["local"])
    except Exception:
        ok_local = False
    if cfg.get("engine") == "local" and not ok_local:
        ids = [r.get("id") for r in cfg.get("remotes") or []]
        if ids:
            cfg["engine"] = ids[0]
            config.save(cfg)
            print(f"[deploy] 本地模型不可用，已切换到在线引擎：{ids[0]}")
    if first_run:
        # 首次运行：没有 API Key 但本地权重可用时，默认落到本地模型，开箱即可对话
        ok_local, _ = local.available(cfg["local"])
        has_key = any((r.get("api_key") or "").strip() for r in cfg.get("remotes") or [])
        if ok_local and not has_key:
            cfg["engine"] = "local"
            config.save(cfg)
            print("首次运行：未检测到 API Key，默认使用本地 MiniMind 模型")
        # 训练过的新权重优先
        names = {w["name"] for w in local.list_weights(cfg["local"])}
        for cand in ("chat", "chat_full", "full_sft"):
            if cand in names:
                cfg["local"]["weight"] = cand
                config.save(cfg)
                break

    # 有账号之后，把没有归属的历史会话划给第一个账号，并在需要时补一个示例会话
    users = auth.list_users()
    if users:
        first = sorted(users, key=lambda u: u.get("created") or 0)[0]
        store.claim_ownerless(first["id"])
        ensure_seed_for(first["id"])
    print(f"  账号：{len(users)} 个" + ("" if users else "（首次打开浏览器会引导注册）"))

    try:
        httpd = ChatServer((args.host, args.port), Handler)
    except OSError as e:
        print(f"\n启动失败：端口 {args.port} 无法绑定（{e}）。")
        print(f"多半是已经有一个实例在跑，直接打开 http://{args.host}:{args.port}/ 即可；")
        print(f"或者换个端口：python server.py --port {args.port + 1}")
        sys.exit(1)

    urls = [f"http://{args.host}:{args.port}/"]
    servers = [httpd]

    # 很多浏览器会把 localhost 先解析成 IPv6 的 ::1，这里补一个 IPv6 监听，
    # 这样 127.0.0.1 和 localhost 都能打开，少一类"莫名其妙打不开"。
    if args.host in ("127.0.0.1", "localhost"):
        try:
            class ChatServerV6(ChatServer):
                address_family = socket.AF_INET6

            s6 = ChatServerV6(("::1", args.port), Handler)
            servers.append(s6)
            urls.append(f"http://localhost:{args.port}/")
        except OSError:
            pass  # 系统没启用 IPv6 就算了，不影响 IPv4

    print("=" * 60)
    print("  AI 对话已启动：" + "  或  ".join(urls))
    print(f"  本地模型：{config.load()['local']['minimind_dir']}")
    print("=" * 60)

    if len(servers) > 1:
        for s in servers[1:]:
            threading.Thread(target=s.serve_forever, daemon=True).start()

    if not args.no_open and os.environ.get("AI_CHAT_NO_OPEN") != "1":
        try:
            import webbrowser

            threading.Timer(0.8, lambda: webbrowser.open(urls[0])).start()
        except Exception:
            pass
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
