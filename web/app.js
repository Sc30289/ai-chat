/* AI 对话前端：会话管理 + SSE 流式 + 记忆面板 + 设置。 */
const $ = (id) => document.getElementById(id);

const state = {
  cfg: null,
  me: null,
  engines: {},
  sessions: [],
  current: null,     // {id,title,messages,summary,summary_upto}
  engine: "online",
  streaming: false,
  abort: null,
  typingEl: null,
};

/* ---------------- 弹窗 ---------------- */
function openModal({ title, fields, okText = "确定", submit }) {
  const mask = $("modalMask");
  const form = $("modalForm");
  const body = $("modalBody");
  const errEl = $("modalErr");
  const okBtn = $("modalOk");

  $("modalTitle").textContent = title;
  okBtn.textContent = okText;
  errEl.textContent = "";
  body.innerHTML = "";
  for (const f of fields) {
    const label = document.createElement("label");
    label.textContent = f.label;
    const input = document.createElement("input");
    input.id = "mf_" + f.id;
    input.type = f.type || "text";
    input.value = f.value || "";
    input.placeholder = f.placeholder || "";
    input.autocomplete = f.autocomplete || "off";
    label.appendChild(input);
    body.appendChild(label);
  }
  mask.hidden = false;
  const first = body.querySelector("input");
  if (first) {
    first.focus();
    if (first.value) first.select();
  }

  return new Promise((resolve) => {
    const values = () => {
      const out = {};
      for (const f of fields) out[f.id] = $("mf_" + f.id).value.trim();
      return out;
    };
    const close = (result) => {
      mask.hidden = true;
      form.onsubmit = null;
      $("modalCancel").onclick = null;
      resolve(result);
    };
    form.onsubmit = async (e) => {
      e.preventDefault();
      errEl.textContent = "";
      okBtn.disabled = true;
      try {
        const msg = await submit(values());
        if (msg) {
          errEl.textContent = msg;
          return;
        }
        close(values());
      } catch (e2) {
        errEl.textContent = e2.message || "操作失败";
      } finally {
        okBtn.disabled = false;
      }
    };
    $("modalCancel").onclick = () => close(null);
  });
}

/* ---------------- 小工具 ---------------- */
function toast(msg, isErr, ms) {
  const el = $("toast");
  el.textContent = msg;
  el.className = "toast" + (isErr ? " err" : "");
  el.hidden = false;
  clearTimeout(el._t);
  el._t = setTimeout(() => (el.hidden = true), ms || (isErr ? 5200 : 2600));
}

const TOKEN_KEY = "aic_token";

function getToken() {
  try { return localStorage.getItem(TOKEN_KEY) || ""; } catch (e) { return ""; }
}

function setToken(t) {
  try { t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY); } catch (e) { /* 忽略 */ }
}

function authHeaders(extra) {
  const t = getToken();
  const h = Object.assign({}, extra || {});
  if (t) h["X-Auth-Token"] = t;
  return h;
}

async function api(path, opts) {
  opts = opts || {};
  opts.headers = authHeaders(opts.headers);
  const res = await fetch(path, opts);
  if (res.status === 401) {
    showAuthGate("登录已过期，请重新登录");
    throw new Error("未登录");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.ok === false) throw new Error(data.error || `请求失败（${res.status}）`);
  return data;
}

function esc(s) {
  return String(s || "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* 轻量 markdown：围栏代码 / 行内代码 / 粗体 / 斜体 / 标题 / 列表 / 链接 */
function md(src) {
  const blocks = [];
  let text = String(src || "").replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) => {
    blocks.push(
      `<pre><code data-lang="${esc(lang)}">${esc(code.replace(/\n$/, ""))}</code></pre>`
    );
    return `\u0000B${blocks.length - 1}\u0000`;
  });

  text = esc(text);
  const lines = text.split("\n");
  let out = "";
  let list = null;

  const inline = (s) =>
    s
      .replace(/`([^`]+)`/g, (_, c) => `<code>${c}</code>`)
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>")
      .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>')
      .replace(/\u0000B(\d+)\u0000/g, (_, i) => blocks[+i]);

  for (const line of lines) {
    const h = line.match(/^(#{1,3})\s+(.*)$/);
    const li = line.match(/^\s*(?:[-*•]|\d+\.)\s+(.*)$/);
    if (h) {
      if (list) { out += `</${list}>`; list = null; }
      out += `<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`;
    } else if (li) {
      if (!list) { list = "ul"; out += "<ul>"; }
      out += `<li>${inline(li[1])}</li>`;
    } else {
      if (list) { out += `</${list}>`; list = null; }
      if (line.trim() === "") out += "";
      else out += `<p>${inline(line)}</p>`;
    }
  }
  if (list) out += `</${list}>`;
  return out || "<p></p>";
}

/* ---------------- 渲染 ---------------- */
function renderSessions() {
  const box = $("sessions");
  box.innerHTML = "";
  if (!state.sessions.length) {
    box.innerHTML = '<div style="color:#5d6679;font-size:12px;padding:8px 10px">还没有会话</div>';
    return;
  }
  for (const s of state.sessions) {
    const el = document.createElement("div");
    el.className = "session" + (state.current && state.current.id === s.id ? " active" : "");
    el.innerHTML = `<div class="st">${esc(s.title || "未命名")}</div>
      <div class="sp">${esc(s.preview || (s.has_summary ? "🧠 有摘要" : "空会话"))}</div>
      <button class="del" title="删除">✕</button>`;
    el.onclick = (e) => {
      if (e.target.classList.contains("del")) return;
      openSession(s.id);
    };
    el.querySelector(".del").onclick = async (e) => {
      e.stopPropagation();
      if (!confirm(`删除会话「${s.title}」？`)) return;
      await api(`/api/sessions/${s.id}`, { method: "DELETE" });
      if (state.current && state.current.id === s.id) {
        state.current = null;
        renderMessages();
      }
      await refreshState();
    };
    box.appendChild(el);
  }
}

function emptyState() {
  const hints = state.engine === "local"
    ? ["你好，介绍一下你自己", "哪个敢钻进瀑布，把泉水的源头找出来？", "你叫什么名字？"]
    : ["用三句话解释什么是 Transformer", "帮我把这段话改得更正式一些", "给我一个 TypeScript 的防抖函数"];
  return `<div class="empty">
    <h3>${state.engine === "local" ? "本地 MiniMind 64M" : "在线大模型"} · 记得住上文</h3>
    <p>同一个会话里的内容会作为上下文带给你；聊长了会自动压缩成长期记忆。</p>
    <div class="chips">${hints.map((h) => `<button class="chip">${esc(h)}</button>`).join("")}</div>
  </div>`;
}

function renderMessages() {
  const box = $("messages");
  const s = state.current;
  if (!s || !s.messages.length) {
    box.innerHTML = emptyState();
    box.querySelectorAll(".chip").forEach((b) => {
      b.onclick = () => {
        $("input").value = b.textContent;
        autoGrow();
        send();
      };
    });
    renderMemory();
    $("chatTitle").textContent = s ? s.title || "新对话" : "新对话";
    $("chatMeta").textContent = "";
    return;
  }
  box.innerHTML = "";
  for (const m of s.messages) box.appendChild(bubble(m.role, m.content));
  renderMemory();
  $("chatTitle").textContent = s.title || "新对话";
  const turns = s.messages.filter((m) => m.role === "user").length;
  $("chatMeta").textContent = `${turns} 轮 · 共 ${s.messages.length} 条消息`;
  scrollBottom();
}

function bubble(role, content) {
  const el = document.createElement("div");
  el.className = "msg " + role;
  el.innerHTML = `
    <div class="avatar">${role === "user" ? "我" : "AI"}</div>
    <div class="bubble">
      <div class="body"></div>
      <div class="meta-row"></div>
    </div>`;
  const body = el.querySelector(".body");
  const meta = el.querySelector(".meta-row");
  if (role === "user") {
    body.innerHTML = `<p>${esc(content).replace(/\n/g, "<br/>")}</p>`;
  } else {
    body.innerHTML = md(content);
    const copy = document.createElement("button");
    copy.className = "mini-btn";
    copy.textContent = "复制";
    copy.onclick = () => navigator.clipboard.writeText(content).then(() => toast("已复制"));
    meta.appendChild(copy);
    const regen = document.createElement("button");
    regen.className = "mini-btn";
    regen.textContent = "重新生成";
    regen.onclick = () => regenerate();
    meta.appendChild(regen);
    if (content) {
      const len = document.createElement("span");
      len.textContent = `${content.length} 字`;
      meta.appendChild(len);
    }
  }
  return el;
}

function renderMemory() {
  const s = state.current;
  const bar = $("memoryBar");
  const card = $("memoryCard");
  const summary = (s && s.summary) || "";
  const turns = s ? s.messages.filter((m) => m.role === "user").length : 0;
  const windowTurns = state.cfg ? state.cfg.memory.window_turns : 6;

  if (!s || (!summary && turns <= windowTurns)) {
    bar.hidden = true;
    card.hidden = true;
    return;
  }
  bar.hidden = false;
  if (state.engine === "local") {
    // 本地 64M 小模型装不下摘要，只保留最近几轮原文
    $("memText").textContent = `本地小模型：只带最近 ${Math.min(turns, windowTurns)} 轮原文（摘要不注入）`;
    $("toggleMem").hidden = !summary;
    $("toggleMem").textContent = card.hidden ? "查看摘要" : "收起摘要";
    $("memBody").textContent = summary;
    return;
  }
  const covered = s.summary_upto ? Math.floor(s.summary_upto / 2) : 0;
  $("memText").textContent = summary
    ? `长期记忆已生效：前面约 ${covered} 轮已压缩成摘要，本次请求会一起带上`
    : `当前上下文窗口：最近 ${Math.min(turns, windowTurns)} 轮`;
  $("toggleMem").hidden = !summary;
  $("toggleMem").textContent = card.hidden ? "展开摘要" : "收起摘要";
  $("memBody").textContent = summary;
}

function scrollBottom() {
  const box = $("messages");
  box.scrollTop = box.scrollHeight;
}

/* ---------------- 状态 ---------------- */
async function refreshState() {
  const data = await api("/api/state");
  state.cfg = data.config;
  state.me = data.me || state.me;
  state.engines = data.engines;
  state.remotes = data.remotes || (data.config && data.config.remotes) || [];
  state.localWeights = data.local_weights || [];
  state.localLoras = data.local_loras || [];
  state.sessions = data.sessions;
  state.engine = data.config.engine || "local";
  renderUser();
  applyEngineUI();
  renderSessions();
  applySettingsToInputs();
}

function renderUser() {
  const name = (state.me && state.me.username) || "未登录";
  $("userName").textContent = name;
  $("userAvatar").textContent = name.slice(0, 1).toUpperCase();
  $("userChip").title = name + "（点这里改用户名 / 密码 / 退出）";
}

async function renameUser() {
  const cur = (state.me && state.me.username) || "";
  await openModal({
    title: "修改用户名",
    okText: "保存",
    fields: [{ id: "username", label: "新的用户名", value: cur, placeholder: "2~20 个字符" }],
    submit: async (v) => {
      if (!v.username) return "用户名不能为空";
      if (v.username === cur) return "和原来一样，没改动";
      try {
        const d = await api("/api/profile", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ username: v.username }),
        });
        state.me = d.user;
        renderUser();
        toast(d.message || "用户名已修改");
        return null;
      } catch (e) {
        return e.message;
      }
    },
  });
}

async function changePassword() {
  await openModal({
    title: "修改密码",
    okText: "确认修改",
    fields: [
      { id: "old_password", label: "原密码", type: "password", autocomplete: "current-password" },
      { id: "new_password", label: "新密码", type: "password", placeholder: "至少 6 位", autocomplete: "new-password" },
      { id: "confirm", label: "再输一次新密码", type: "password", autocomplete: "new-password" },
    ],
    submit: async (v) => {
      if (!v.old_password) return "请填原密码";
      if ((v.new_password || "").length < 6) return "新密码至少 6 位";
      if (v.new_password !== v.confirm) return "两次输入的新密码不一致";
      try {
        const d = await api("/api/password", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ old_password: v.old_password, new_password: v.new_password }),
        });
        toast(d.message || "密码已修改");
        return null;
      } catch (e) {
        return e.message;
      }
    },
  });
}

async function logout() {
  if (!confirm("确定退出登录？")) return;
  try {
    await fetch("/api/logout", { method: "POST", headers: authHeaders() });
  } catch (e) {
    /* 忽略 */
  }
  setToken("");
  state.me = null;
  state.current = null;
  state.sessions = [];
  $("sessions").innerHTML = "";
  $("messages").innerHTML = "";
  $("userMenu").hidden = true;
  showAuthGate();
  toast("已退出登录");
}

function remoteById(id) {
  return (state.remotes || []).find((r) => r.id === id) || null;
}

function engineName(id) {
  if (id === "local") return "本地 MiniMind";
  const r = remoteById(id);
  return r ? r.name || r.id : id;
}

/* 顶栏按钮：本地 + 每个远端，动态生成 */
function applyEngineUI() {
  const box = $("engineSwitch");
  const ids = ["local"].concat((state.remotes || []).map((r) => r.id));
  const keep = Array.from(box.querySelectorAll("button")).map((b) => b.dataset.engine).join(",");
  if (keep !== ids.join(",")) {
    box.innerHTML = "";
    for (const id of ids) {
      const b = document.createElement("button");
      b.dataset.engine = id;
      b.textContent = engineName(id);
      b.onclick = () => switchEngine(id);
      box.appendChild(b);
    }
  }
  box.querySelectorAll("button").forEach((b) => {
    const name = b.dataset.engine;
    const info = state.engines[name] || {};
    const r = remoteById(name);
    b.classList.toggle("active", name === state.engine);
    b.title = info.available
      ? (r ? `${r.name} · ${r.model || "未填模型"}` : "本地 MiniMind · 离线免费")
      : (info.reason || "不可用");
    b.style.opacity = info.available ? "" : ".5";
    b.textContent = engineName(name);
  });

  const info = state.engines[state.engine] || {};
  $("engineDot").className = "engine-dot " + (info.available ? "on" : "off");
  let label = engineName(state.engine);
  if (!info.available) label += "（不可用）";
  else if (state.engine === "local") {
    const st = info.status || {};
    if (st.loaded) label += " · " + String(st.weight || "").replace(/_768$/, "") + (st.lora && st.lora !== "none" ? " + " + st.lora : "");
    else if (state.cfg) label += " · " + String(state.cfg.local.weight || "").replace(/_768$/, "");
  }
  $("engineLabel").textContent = label;
  $("hint").textContent = state.engine === "local"
    ? "本地 64M 小模型，CPU 上较慢；上下文只保留最近几轮"
    : "回答会参考本会话的最近若干轮内容，以及更早对话的长期记忆摘要";
}

async function switchEngine(id) {
  const info = state.engines[id] || {};
  if (!info.available) {
    toast(`${engineName(id)} 不可用：${info.reason || "未知原因"}`, true);
    return;
  }
  const prev = state.engine;
  state.engine = id;
  applyEngineUI();
  renderMessages();
  try {
    const data = await api("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ engine: id }),
    });
    state.cfg = data.config || state.cfg;
    state.engines = data.engines || state.engines;
    applyEngineUI();
    flashEngine();
    toast(engineTip(id), false, 3200);
  } catch (e) {
    // 存盘失败就退回原来的模式，别让界面和实际状态不一致
    state.engine = prev;
    applyEngineUI();
    toast("切换失败：" + e.message, true);
  }
}

/* 切换模式后的提示文案：带上具体模型 / 参数，让人一眼知道在用哪个 */
function engineTip(id) {
  if (id === "local") {
    const w = String((state.cfg && state.cfg.local.weight) || "").replace(/_768$/, "");
    return `已切到「本地 MiniMind」${w ? " · " + w : ""}（免费、离线，但能力有限）`;
  }
  const r = remoteById(id);
  if (!r) return `已切到「${engineName(id)}」`;
  const model = r.model ? ` · ${r.model}` : "";
  return `已切到「${r.name}」${model}（调用远端接口，会产生费用）`;
}

function flashEngine() {
  const box = $("engineSwitch");
  box.classList.remove("flash");
  void box.offsetWidth; // 强制重排，动画才能重放
  box.classList.add("flash");
  setTimeout(() => box.classList.remove("flash"), 900);
}

/* ---------------- 会话 ---------------- */
async function openSession(id) {
  const data = await api(`/api/sessions/${id}`);
  state.current = data.session;
  renderMessages();
  renderSessions();
  $("input").focus();
}

async function newSession() {
  const data = await api("/api/sessions", { method: "POST" });
  state.current = data.session;
  await refreshState();
  renderMessages();
  $("input").focus();
}

/* ---------------- 发送 / 流式 ---------------- */
function autoGrow() {
  const t = $("input");
  t.style.height = "auto";
  t.style.height = Math.min(t.scrollHeight, 200) + "px";
}

function setSending(on) {
  state.streaming = on;
  const btn = $("send");
  btn.classList.toggle("stop", on);
  btn.textContent = on ? "■" : "↑";
  btn.title = on ? "停止" : "发送";
}

async function send() {
  if (state.streaming) {
    stopStream();
    return;
  }
  const input = $("input");
  const text = input.value.trim();
  if (!text) return;

  if (!state.current) {
    const data = await api("/api/sessions", { method: "POST" });
    state.current = data.session;
  }

  state.current.messages.push({ role: "user", content: text });
  input.value = "";
  autoGrow();
  renderMessages();
  renderSessions();

  await streamChat({ content: text });
}

async function regenerate() {
  if (state.streaming || !state.current) return;
  const msgs = state.current.messages;
  if (msgs.length && msgs[msgs.length - 1].role === "assistant") msgs.pop();
  renderMessages();
  await streamChat({ regenerate: true });
}

function stopStream() {
  if (state.abort) state.abort.abort();
}

async function streamChat(payload) {
  setSending(true);

  const box = $("messages");
  if (box.querySelector(".empty")) box.innerHTML = "";

  const wrap = document.createElement("div");
  wrap.className = "msg assistant";
  wrap.innerHTML = `
    <div class="avatar">AI</div>
    <div class="bubble">
      <div class="status-line" style="display:none"><span class="spinner"></span><span class="stxt"></span></div>
      <div class="body"></div>
      <div class="meta-row"></div>
    </div>`;
  box.appendChild(wrap);
  const body = wrap.querySelector(".body");
  const statusEl = wrap.querySelector(".status-line");
  const stxt = wrap.querySelector(".stxt");
  scrollBottom();

  let acc = "";
  let caret = document.createElement("span");
  caret.className = "caret";
  body.appendChild(caret);

  const render = () => {
    body.innerHTML = md(acc);
    if (acc && !caret.isConnected) body.appendChild(caret);
    if (!acc) body.appendChild(caret);
    scrollBottom();
  };

  const ac = new AbortController();
  state.abort = ac;

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: state.current ? state.current.id : "", engine: state.engine, ...payload }),
      headers: authHeaders({ "Content-Type": "application/json" }),
      signal: ac.signal,
    });
    if (res.status === 401) {
      showAuthGate("登录已过期，请重新登录");
      return;
    }
    if (!res.ok) {
      const j = await res.json().catch(() => ({}));
      throw new Error(j.error || `请求失败（${res.status}）`);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });

      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const raw = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        let event = "message";
        let data = "";
        for (const line of raw.split("\n")) {
          if (line.startsWith("event:")) event = line.slice(6).trim();
          else if (line.startsWith("data:")) data += line.slice(5).trim();
        }
        if (!data) continue;
        let obj = {};
        try { obj = JSON.parse(data); } catch { continue; }

        if (event === "meta") {
          if (obj.session_id && state.current) state.current.id = obj.session_id;
          else if (obj.session_id) await openSession(obj.session_id);
          if (obj.title) {
            state.current.title = obj.title;
            $("chatTitle").textContent = obj.title;
          }
        } else if (event === "status") {
          statusEl.style.display = "flex";
          stxt.textContent = obj.text || "处理中…";
        } else if (event === "delta") {
          statusEl.style.display = "none";
          acc += obj.text || "";
          render();
        } else if (event === "error") {
          statusEl.style.display = "none";
          acc += (acc ? "\n\n" : "") + "⚠️ " + (obj.message || "出错了");
          render();
          toast(obj.message || "出错了", true);
        } else if (event === "memory") {
          if (state.current) {
            state.current.summary = obj.summary || "";
            state.current.summary_upto = obj.covered || 0;
            renderMemory();
          }
          toast("已更新长期记忆摘要 🧠");
        } else if (event === "done") {
          statusEl.style.display = "none";
          caret.remove();
          if (obj.text) {
            acc = obj.text;
            body.innerHTML = md(acc);
          }
          state.current.messages.push({ role: "assistant", content: acc });
          if (obj.elapsed) {
            const meta = wrap.querySelector(".meta-row");
            meta.innerHTML = "";
            const copy = document.createElement("button");
            copy.className = "mini-btn";
            copy.textContent = "复制";
            copy.onclick = () => navigator.clipboard.writeText(acc).then(() => toast("已复制"));
            meta.appendChild(copy);
            const regen = document.createElement("button");
            regen.className = "mini-btn";
            regen.textContent = "重新生成";
            regen.onclick = () => regenerate();
            meta.appendChild(regen);
            const info = document.createElement("span");
            info.textContent = `${acc.length} 字 · ${obj.elapsed}s` + (obj.stopped ? " · 已停止" : "");
            meta.appendChild(info);
          }
          await refreshState();
          if (state.current) await openSession(state.current.id);
        }
      }
    }
  } catch (e) {
    if (e.name === "AbortError") {
      caret.remove();
      toast("已停止生成");
      // 服务端会把已生成的部分存下来，这里直接以服务端为准重新同步
      if (state.current) {
        await refreshState();
        await openSession(state.current.id);
      }
    } else {
      statusEl.style.display = "none";
      toast(e.message || "请求失败", true);
      if (!acc) wrap.remove();
    }
  } finally {
    setSending(false);
    state.abort = null;
    scrollBottom();
  }
}

/* ---------------- 设置 ---------------- */
function renderRemotes() {
  const box = $("remoteList");
  const list = state.remotes || [];
  box.innerHTML = "";
  if (!list.length) {
    box.innerHTML = '<p class="tip">还没有远端接口，从下面选一个预设添加。</p>';
  }
  for (const r of list) {
    const card = document.createElement("div");
    card.className = "remote-card";
    card.dataset.id = r.id;
    card.innerHTML = `
      <div class="rc-head">
        <input class="rc-name" value="${esc(r.name || r.id)}" placeholder="名称" />
        <button class="rc-del" type="button" title="删除这个远端">✕</button>
      </div>
      <span class="rc-badge ${r.has_api_key ? "ok" : ""}">${r.has_api_key ? "Key 已保存 " + esc(r.api_key_hint || "") : "未配置 Key"}</span>
      <label>Base URL<input class="rc-url" value="${esc(r.base_url || "")}" placeholder="https://api.deepseek.com" /></label>
      <label>模型<input class="rc-model" value="${esc(r.model || "")}" placeholder="deepseek-chat" /></label>
      <label>API Key<input class="rc-key" type="password" placeholder="${r.has_api_key ? "留空表示不修改" : "粘贴 Key"}" autocomplete="off" /></label>
      <div class="test-row">
        <button class="btn small rc-test" type="button">测试连接</button>
        <span class="test-result rc-result"></span>
      </div>`;
    card.querySelector(".rc-test").onclick = () => testRemote(card);
    card.querySelector(".rc-del").onclick = () => {
      if ((state.remotes || []).length <= 1) {
        toast("至少保留一个远端接口", true);
        return;
      }
      card.remove();
    };
    box.appendChild(card);
  }

  const sel = $("remotePreset");
  sel.innerHTML = "";
  for (const p of (state.cfg && state.cfg.presets) || []) {
    const o = document.createElement("option");
    o.value = p.id;
    o.textContent = p.name + (p.model ? ` · ${p.model}` : "");
    sel.appendChild(o);
  }
}

async function testRemote(card) {
  const el = card.querySelector(".rc-result");
  const btn = card.querySelector(".rc-test");
  el.className = "test-result rc-result";
  el.textContent = "测试中…";
  btn.disabled = true;
  try {
    const res = await fetch("/api/test_online", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        remote_id: card.dataset.id,
        base_url: card.querySelector(".rc-url").value.trim(),
        model: card.querySelector(".rc-model").value.trim(),
        api_key: card.querySelector(".rc-key").value.trim(),
      }),
    });
    const data = await res.json();
    el.className = "test-result rc-result " + (data.connected ? "ok" : "err");
    el.textContent = (data.connected ? "✅ " : "❌ ") + data.message;
  } catch (e) {
    el.className = "test-result rc-result err";
    el.textContent = "❌ 请求失败：" + e.message;
  } finally {
    btn.disabled = false;
  }
}

function addRemote() {
  const pid = $("remotePreset").value;
  const preset = ((state.cfg && state.cfg.presets) || []).find((x) => x.id === pid)
    || { id: "custom", name: "自定义", base_url: "", model: "" };
  const used = new Set((state.remotes || []).map((r) => r.id));
  let id = preset.id;
  let n = 1;
  while (used.has(id)) {
    n += 1;
    id = preset.id + n;
  }
  state.remotes = (state.remotes || []).concat([{
    id,
    name: preset.name,
    base_url: preset.base_url || "",
    model: preset.model || "",
    has_api_key: false,
    api_key_hint: "",
    temperature: 0.7,
  }]);
  renderRemotes();
}

function collectRemotes() {
  return Array.from($("remoteList").querySelectorAll(".remote-card")).map((card) => {
    const out = {
      id: card.dataset.id,
      name: card.querySelector(".rc-name").value.trim(),
      base_url: card.querySelector(".rc-url").value.trim(),
      model: card.querySelector(".rc-model").value.trim(),
    };
    const key = card.querySelector(".rc-key").value.trim();
    if (key) out.api_key = key;
    return out;
  });
}

function applySettingsToInputs() {
  const c = state.cfg;
  if (!c) return;
  state.remotes = c.remotes || state.remotes || [];
  renderRemotes();
  fillWeights(c.local.weight);
  fillLoras(c.local.lora);
  $("cfgMaxNew").value = c.local.max_new_tokens;
  $("cfgPromptTok").value = c.local.max_prompt_tokens;
  $("cfgMemEnabled").checked = !!(c.memory && c.memory.enabled);
  $("cfgWindow").value = c.memory.window_turns;
  $("cfgTrigger").value = c.memory.summary_trigger_turns;
  $("cfgSystem").value = c.system_prompt || "";
}

function fillWeights(current) {
  const sel = $("cfgWeight");
  const list = state.localWeights || [];
  sel.innerHTML = "";
  for (const w of list) {
    const opt = document.createElement("option");
    opt.value = w.name;
    opt.textContent = `${w.name}（${w.size_mb} MB）`;
    sel.appendChild(opt);
  }
  if (current && !list.some((w) => w.name === current)) {
    const opt = document.createElement("option");
    opt.value = current;
    opt.textContent = `${current}（未找到）`;
    sel.appendChild(opt);
  }
  sel.value = current || (list[0] && list[0].name) || "";
}

function fillLoras(current) {
  const sel = $("cfgLora");
  const list = state.localLoras || [];
  sel.innerHTML = "";
  const none = document.createElement("option");
  none.value = "none";
  none.textContent = "不用 LoRA（纯 SFT）";
  sel.appendChild(none);
  for (const name of list) {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    sel.appendChild(opt);
  }
  if (current && current !== "none" && !list.includes(current)) {
    const opt = document.createElement("option");
    opt.value = current;
    opt.textContent = `${current}（未找到）`;
    sel.appendChild(opt);
  }
  sel.value = current || "none";
}

function openDrawer() {
  applySettingsToInputs();
  $("drawer").hidden = false;
  $("mask").hidden = false;
}

function closeDrawer() {
  $("drawer").hidden = true;
  $("mask").hidden = true;
}

async function saveSettings() {
  const remotes = collectRemotes();
  const patch = {
    system_prompt: $("cfgSystem").value,
    remotes,
    local: {
      weight: $("cfgWeight").value || "full_sft",
      lora: $("cfgLora").value,
      max_new_tokens: parseInt($("cfgMaxNew").value, 10) || 96,
      max_prompt_tokens: parseInt($("cfgPromptTok").value, 10) || 320,
    },
    memory: {
      enabled: $("cfgMemEnabled").checked,
      window_turns: parseInt($("cfgWindow").value, 10) || 6,
      summary_trigger_turns: parseInt($("cfgTrigger").value, 10) || 10,
    },
  };

  try {
    const data = await api("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    try {
      state.cfg = data.config;
      state.engines = data.engines;
      state.remotes = data.remotes || data.config.remotes || [];
      state.localWeights = data.local_weights || state.localWeights;
      state.localLoras = data.local_loras || state.localLoras;
      state.sessions = data.sessions;
      state.engine = data.config.engine || state.engine;
    } catch (uiErr) {
      console.error("保存后刷新界面失败", uiErr);
    }

    // 刚给某个远端填了 Key：顺手切过去，省得还要手动点
    let autoSwitched = false;
    const keyed = remotes.filter((r) => r.api_key).map((r) => r.id);
    if (keyed.length && state.engine === "local") {
      try {
        const r2 = await api("/api/config", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ engine: keyed[0] }),
        });
        state.engine = keyed[0];
        state.cfg = r2.config;
        state.engines = r2.engines;
        state.remotes = r2.remotes || state.remotes;
        autoSwitched = true;
      } catch (e2) {
        console.error("自动切换远端失败", e2);
      }
    }

    // 保存成功就一定把面板收起来，别让界面小毛病把用户困在里面
    closeDrawer();
    applyEngineUI();
    if (autoSwitched) {
      toast(`已保存，并自动切到「${engineName(state.engine)}」`);
    } else {
      toast("设置已保存");
    }
  } catch (e) {
    toast(e.message, true);
  }
}

/* ---------------- 事件绑定 ---------------- */
function bind() {
  $("newChat").onclick = newSession;
  $("openSettings").onclick = openDrawer;
  $("btnSettings").onclick = openDrawer;
  $("closeDrawer").onclick = closeDrawer;
  $("mask").onclick = closeDrawer;
  $("addRemote").onclick = addRemote;

  // 用户菜单
  $("userChip").onclick = (e) => {
    if (e.target.closest(".user-menu")) return;
    const menu = $("userMenu");
    menu.hidden = !menu.hidden;
  };
  $("userMenu").onclick = (e) => {
    const act = e.target.dataset && e.target.dataset.act;
    if (!act) return;
    $("userMenu").hidden = true;
    if (act === "rename") renameUser();
    else if (act === "password") changePassword();
    else if (act === "logout") logout();
  };
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#userChip")) $("userMenu").hidden = true;
  });
  $("modalMask").onclick = (e) => {
    if (e.target === $("modalMask")) $("modalCancel").click();
  };

  $("saveSettings").onclick = saveSettings;
  $("cancelSettings").onclick = closeDrawer;

  $("send").onclick = send;

  const input = $("input");
  input.addEventListener("input", autoGrow);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      send();
    }
  });

  $("toggleMem").onclick = () => {
    const card = $("memoryCard");
    card.hidden = !card.hidden;
    $("toggleMem").textContent = card.hidden ? "展开摘要" : "收起摘要";
  };

  document.querySelectorAll("#engineSwitch button").forEach((b) => {
    b.onclick = async () => {
      const name = b.dataset.engine;
      const info = state.engines[name] || {};
      if (!info.available) {
        toast(`${name === "local" ? "本地模型" : "在线接口"}不可用：${info.reason || "未知原因"}`, true);
        return;
      }
      state.engine = name;
      applyEngineUI();
      renderMessages();
      try {
        await api("/api/config", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ engine: name }),
        });
      } catch (e) {
        toast(e.message, true);
      }
    };
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      closeDrawer();
      $("userMenu").hidden = true;
      if (!$("modalMask").hidden) $("modalCancel").click();
    }
  });

  // 登录 / 注册
  document.querySelectorAll(".auth-tab").forEach((t) => {
    t.onclick = () => setAuthMode(t.dataset.mode);
  });
  $("authForm").onsubmit = (e) => {
    e.preventDefault();
    submitAuth();
  };
}

/* ---------------- 登录 / 注册（页内遮罩） ---------------- */
let authMode = "login";

function showAuthGate(msg) {
  $("authGate").hidden = false;
  $("auErr").textContent = msg || "";
  const name = (state.me && state.me.username) || "";
  $("auName").value = name;
  $("auPass").value = "";
  $("auConfirm").value = "";
  setTimeout(() => ($("auName").value ? $("auPass").focus() : $("auName").focus()), 30);
}

function hideAuthGate() {
  $("authGate").hidden = true;
  $("auErr").textContent = "";
}

function setAuthMode(mode) {
  authMode = mode;
  document.querySelectorAll(".auth-tab").forEach((t) => {
    t.classList.toggle("active", t.dataset.mode === mode);
  });
  $("auConfirmWrap").hidden = mode !== "register";
  $("auPass").autocomplete = mode === "register" ? "new-password" : "current-password";
  $("auSubmit").textContent = mode === "register" ? "注册并进入" : "登录";
  $("auHint").textContent = mode === "register"
    ? "注册后会自动登录；账号只保存在本机。"
    : "还没有账号？点上面的「注册」创建一个。";
  $("auErr").textContent = "";
}

async function submitAuth() {
  const username = $("auName").value.trim();
  const password = $("auPass").value;
  const confirm = $("auConfirm").value;
  const err = $("auErr");
  err.textContent = "";

  if (!username) { err.textContent = "请填用户名"; return; }
  if (password.length < 6) { err.textContent = "密码至少 6 位"; return; }
  if (authMode === "register" && password !== confirm) {
    err.textContent = "两次输入的密码不一致";
    return;
  }

  const btn = $("auSubmit");
  btn.disabled = true;
  btn.textContent = authMode === "register" ? "注册中…" : "登录中…";
  try {
    const res = await fetch(authMode === "register" ? "/api/register" : "/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password, confirm: authMode === "register" ? confirm : undefined }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.ok === false) throw new Error(data.error || `请求失败（${res.status}）`);
    setToken(data.token || "");
    state.me = data.user;
    renderUser();
    hideAuthGate();
    await startApp();
    toast(authMode === "register" ? "注册成功，已登录" : `登录成功：${data.user.username}`);
  } catch (e) {
    err.textContent = e.message;
  } finally {
    btn.disabled = false;
    btn.textContent = authMode === "register" ? "注册并进入" : "登录";
  }
}

async function startApp() {
  try {
    await refreshState();
    if (state.sessions.length) {
      await openSession(state.sessions[0].id);
    } else {
      renderMessages();
    }
  } catch (e) {
    toast("初始化失败：" + e.message, true);
  }
  $("input").focus();
}

async function boot() {
  bind();
  setAuthMode("login");
  let me = null;
  try {
    const res = await fetch("/api/me", { headers: authHeaders() });
    const data = await res.json();
    me = data.user;
  } catch (e) {
    me = null;
  }
  if (!me) {
    showAuthGate();
    return;
  }
  state.me = me;
  renderUser();
  hideAuthGate();
  await startApp();
}

boot();
