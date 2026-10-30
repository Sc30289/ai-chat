"""账号与登录态：本地账号表 + PBKDF2 口令哈希 + Cookie 令牌。

存储（都在 data/ 下，已在 .gitignore 里）：
    users.json  {"users": [{id, username, salt, pwd_hash, created}]}
    auth.json   {"tokens": {token: {uid, created, seen}}}

只用标准库，够这个本机小应用用了。注意：本服务默认只监听 127.0.0.1，
如果以后对外开端口，请自己加 HTTPS（Cookie 没有 Secure 标记）。
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

from config import DATA_DIR

USERS_PATH = os.path.join(DATA_DIR, "users.json")
AUTH_PATH = os.path.join(DATA_DIR, "auth.json")

ITERATIONS = 200_000
TOKEN_TTL = 7 * 24 * 3600  # 7 天
COOKIE_NAME = "aic_auth"
USERNAME_RE = re.compile(r"^[\w\u4e00-\u9fff.\- ]{2,20}$")

_LOCK = threading.Lock()


class AuthError(ValueError):
    pass


# ---------------- 底层读写 ----------------
def _load(path, key):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except (OSError, ValueError):
        data = {}
    data.setdefault(key, {})
    return data


def _save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                               bytes.fromhex(salt), ITERATIONS).hex()


# ---------------- 用户 ----------------
def list_users():
    return list(_users().values())


def _users():
    raw = _load(USERS_PATH, "users")
    return raw["users"]


def _save_users(users):
    _save(USERS_PATH, {"users": users})


def find_by_name(username):
    name = (username or "").strip().lower()
    for u in _users().values():
        if u["username"].strip().lower() == name:
            return u
    return None


def find_by_id(uid):
    return _users().get(uid)


def count_users():
    return len(_users())


def _validate(username, password):
    username = (username or "").strip()
    if not USERNAME_RE.match(username):
        raise AuthError("用户名要 2~20 个字符，只能用中英文、数字、下划线、点、横线、空格")
    if len(password or "") < 6:
        raise AuthError("密码至少 6 位")
    if len(password) > 128:
        raise AuthError("密码太长了")


def create_user(username, password):
    username = (username or "").strip()
    _validate(username, password)
    with _LOCK:
        users = _users()
        if any(u["username"].strip().lower() == username.lower() for u in users.values()):
            raise AuthError("这个用户名已经被注册了")
        salt = secrets.token_hex(16)
        uid = "u_" + secrets.token_hex(8)
        users[uid] = {
            "id": uid,
            "username": username,
            "salt": salt,
            "pwd_hash": _hash(password, salt),
            "created": time.time(),
        }
        _save_users(users)
        return users[uid]


def check_password(user, password):
    if not user:
        return False
    return hmac.compare_digest(_hash(password or "", user["salt"]), user["pwd_hash"])


def rename_user(user, new_name):
    new_name = (new_name or "").strip()
    if not USERNAME_RE.match(new_name):
        raise AuthError("用户名要 2~20 个字符，只能用中英文、数字、下划线、点、横线、空格")
    with _LOCK:
        users = _users()
        for u in users.values():
            if u["id"] != user["id"] and u["username"].strip().lower() == new_name.lower():
                raise AuthError("这个用户名已经被占用了")
        users[user["id"]]["username"] = new_name
        _save_users(users)
        return users[user["id"]]


def set_password(user, new_password):
    if len(new_password or "") < 6:
        raise AuthError("新密码至少 6 位")
    with _LOCK:
        users = _users()
        salt = secrets.token_hex(16)
        users[user["id"]]["salt"] = salt
        users[user["id"]]["pwd_hash"] = _hash(new_password, salt)
        _save_users(users)


# ---------------- 登录令牌 ----------------
def create_token(uid):
    with _LOCK:
        data = _load(AUTH_PATH, "tokens")
        tokens = data["tokens"]
        now = time.time()
        for t in [t for t, v in tokens.items() if now - v.get("seen", v.get("created", 0)) > TOKEN_TTL]:
            tokens.pop(t, None)
        token = secrets.token_urlsafe(32)
        tokens[token] = {"uid": uid, "created": now, "seen": now}
        _save(AUTH_PATH, data)
        return token


def user_by_token(token):
    if not token:
        return None
    with _LOCK:
        data = _load(AUTH_PATH, "tokens")
        info = data["tokens"].get(token)
        if not info:
            return None
        now = time.time()
        if now - info.get("seen", info.get("created", 0)) > TOKEN_TTL:
            data["tokens"].pop(token, None)
            _save(AUTH_PATH, data)
            return None
        info["seen"] = now
        _save(AUTH_PATH, data)
        return find_by_id(info.get("uid"))


def drop_token(token):
    if not token:
        return
    with _LOCK:
        data = _load(AUTH_PATH, "tokens")
        if data["tokens"].pop(token, None) is not None:
            _save(AUTH_PATH, data)


def drop_user_tokens(uid, keep=None):
    with _LOCK:
        data = _load(AUTH_PATH, "tokens")
        changed = False
        for t, v in list(data["tokens"].items()):
            if v.get("uid") == uid and t != keep:
                data["tokens"].pop(t)
                changed = True
        if changed:
            _save(AUTH_PATH, data)
