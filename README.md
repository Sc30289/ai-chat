# AI 对话（记忆增强版）

一个能在浏览器里聊天的 AI 助手，**记得住之前的对话**：同一个会话里最近几轮原文直接作为上下文，
更早的内容自动压缩成"长期记忆摘要"常驻 system prompt。支持**在线大模型 API** 和**本地 miniMind 64M 小模型**两种后端，界面上随时切换。

```
ai-chat/
├─ server.py           # 标准库 HTTP + SSE 服务端（无第三方依赖）
├─ config.py           # data/config.json 读写
├─ store.py            # 会话持久化（data/sessions/*.json）
├─ memory.py           # 滑动窗口 + 滚动摘要
├─ engines/
│   ├─ online.py       # OpenAI 兼容 /chat/completions 流式
│   └─ local.py        # 本地 miniMind（torch，流式逐字返回）
├─ web/                # 前端（原生 HTML/CSS/JS，无构建）
├─ data/               # 运行时生成：配置与会话
├─ .vscode/launch.json # F5 一键启动/调试
├─ 启动.bat             # Windows 双击启动
└─ start.ps1           # PowerShell 启动（等价，可传 -Port）
```

## 快速开始

```powershell
cd ai-chat
python server.py
```

浏览器会自动打开 <http://127.0.0.1:8765/>。

### 启动方式说明

**1. 命令行（最简单，`python` 是哪个都行）**——就是上面这两行。

服务启动时会自己检查解释器：如果当前 `python` 没装 torch，而 `../minimind-vscode/.venv` 存在，
就自动用那个解释器重启自己（打印一行 `[launcher] ... switching to:`），所以本地模型照样能跑。
加参数：`python server.py --port 8900 --host 0.0.0.0 --no-open`（`--no-open` = 不自动开浏览器）。

**2. 双击 `启动.bat`**（Windows，自动挑 venv 里的 python，结束后停留窗口便于看报错）

**3. VS Code 里按 F5**：`.vscode/launch.json` 已配好，选「▶ 启动 AI 对话（自动开浏览器）」即可断点调试。

**4. PowerShell 封装**：`powershell -ExecutionPolicy Bypass -File start.ps1 -Port 8900`（和方式 1 等价）。

停止：终端里按 `Ctrl+C`。若提示端口被占用，说明已经有一个实例在跑，直接打开
<http://127.0.0.1:8765/> 就行，或者换个 `--port`。

> 纯在线使用（不跑本地模型）时，除了 Python 3.9+ 什么都不需要装——服务端只用标准库。

首次运行会在本机检测：没有 API Key 但本地权重可用时，默认引擎直接落成「本地 MiniMind」，打开就能对话；
同时会生成一个示例会话演示记忆效果。

### 配置在线模型

右上角 ⚙ → 填 Base URL 和 API Key（默认 DeepSeek：`https://api.deepseek.com` + `deepseek-chat`），保存即可。
也可以不落盘：

```powershell
$env:DEEPSEEK_API_KEY = "sk-..."
python server.py
```

换其它厂商只改 Base URL 和模型名即可（Moonshot、硅基流动、本地 vLLM/Ollama 的 OpenAI 兼容端点都行）。

### 用本地 miniMind

在设置里把引擎切成「本地 MiniMind」，然后在「模型权重」下拉里选：

| 权重 | 说明 |
| --- | --- |
| `chat` | **推荐**。按课程 SFT+LoRA 流程重新训的成品（LoRA 已合并），多轮追问、自我介绍、角色扮演、承认不知道都比较稳 |
| `chat_full` | 上面那个的 SFT 基座（没合并 LoRA） |
| `full_sft` | 早期只用 78 条通用数据训的版本 |
| `pretrain` | 预训练基座，只会续写，不适合对话 |

可再叠 `lora_sunwukong` / `lora_identity` / `lora_chat` 旁路。CPU 上首次加载 5~15 秒，之后每字几十毫秒；
上下文窗口小（默认 320 token），所以本地模式下只带最近几轮原文，不注入摘要。

> 训练细节与效果对比见 `../minimind-vscode/logs/TRAIN_REPORT.md`。
> 本地引擎默认**贪心解码**（`local.greedy=true`）：64M 模型采样很容易胡言乱语；
> 并且本地引擎用的是训练时那句短 system（`local.system_prompt`），避免长 prompt 把模型带偏。

## "根据之前的对话完善"是怎么做的

| 层 | 位置 | 作用 |
| --- | --- | --- |
| 窗口记忆 | `memory.build_context()` | 每次请求带最近 `window_turns` 轮原文（默认 6 轮） |
| 长期记忆 | `memory.update_summary()` | 超出窗口的旧消息压缩成 ≤700 字摘要，写进 system prompt |
| 滚动更新 | `server.api_chat()` 末尾 | 待压缩消息超过 `summary_trigger_turns` 时自动重算摘要 |
| 预算裁剪 | `memory.trim_to_budget()` | 本地小模型按 token 预算从最早的开始丢，保住最新轮次 |

界面上的紫色横条会显示"长期记忆已生效 / 覆盖了前 N 轮"，点「展开摘要」能看到模型实际拿到的记忆内容。

摘要优先用在线模型生成（质量好）；没有 API Key 时退化为抽取式摘要（挑用户说过的话 + 助手结论首句）。
可以在设置里把「启用长期记忆」关掉，退回纯窗口模式。

## 账号与登录

打开 <http://127.0.0.1:8765/> **不会跳页**：登录/注册框直接盖在聊天页上，登录成功后遮罩消失就能开始聊。

- **注册**：用户名 2~20 字符（中英文/数字/下划线/点/横线/空格），密码至少 6 位；注册成功自动登录
- **首次注册**：会自动接管你之前那些"没有归属"的历史会话，并送一个示例会话
- **右上角头像**：点开有三个操作
  - 修改用户名（重名会提示）
  - 修改密码（要验证原密码；改完会把**其它设备**的登录踢掉，当前这个保留）
  - 退出登录（回到遮罩，不刷新页面）
- **会话隔离**：每个会话带 `owner`，不同账号互相看不到对方对话；直接拿别人的会话 id 请求会返回 404

实现说明（`auth.py`）：

| 项目 | 做法 |
| --- | --- |
| 口令存储 | PBKDF2-HMAC-SHA256，20 万轮 + 每用户随机盐，`hmac.compare_digest` 比对 |
| 登录态 | 随机令牌放 `localStorage`，每个请求带 `X-Auth-Token` 头（**不用 Cookie**，避免浏览器 Cookie 策略把它丢掉） |
| 存储位置 | `data/users.json`、`data/auth.json`（都在 .gitignore 里） |

> ⚠️ 这是给本机用的小应用：没有 HTTPS，也没有找回密码流程。忘了密码就直接删掉
> `data/users.json` 重新注册（会话不会丢，新注册的第一个账号会重新认领）。
> 如果想给局域网/公网用，请先套一层 HTTPS 反代，并把 `--host 0.0.0.0` 慎用。
>
> 注意：登录遮罩是前端控制显示，真正的数据保护在后端 —— 所有 `/api/*` 都校验令牌，没令牌一律 401。

## 接口

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/state` | 配置（密钥脱敏）、引擎可用性、会话列表 |
| POST | `/api/config` | 局部更新配置 |
| GET/POST | `/api/sessions` | 列表 / 新建 |
| GET/DELETE/PATCH | `/api/sessions/{id}` | 读取 / 删除 / 改标题或摘要 |
| POST | `/api/chat` | SSE 流式对话，事件：`meta` / `status` / `delta` / `error` / `memory` / `done` |
| POST | `/api/stop` | 停止当前生成 |
| POST | `/api/warmup` | 预加载本地模型 |
| POST | `/api/sessions/{id}/summarize` | 手动重算摘要 |

## 常见问题

- **切到本地模型按钮是灰的**：看侧边栏提示。多半是启动时用的不是 minimind 的 venv（缺 torch），
  或者 `../minimind-vscode/out/full_sft_768.pth` 不在位。
- **在线接口报 401**：Key 不对或没余额；报连接错误检查 Base URL 与代理。
- **端口被占用**：`start.ps1 -Port 8900`。
- **会话存在哪**：`data/sessions/*.json`，直接删文件也能删会话。密钥在 `data/config.json`，别提交到仓库。
