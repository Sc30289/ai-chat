# 部署说明（公网版）

这个应用是「Python 标准库 HTTP 服务 + 原生前端」，不依赖第三方包；只有本地 miniMind 引擎需要 torch 和权重。
公网部署有两种常见方式：

## 方式 A：Render（免费，在线模型）

1. 把本仓库推到 GitHub（已推：`Sc30289/ai-chat`）
2. 打开 <https://render.com/deploy?repo=https://github.com/Sc30289/ai-chat>，登录后按提示创建
   - 平台会自动读取根目录的 `render.yaml`（Docker 运行时、免费套餐）
3. 在环境变量里填 `DEEPSEEK_API_KEY`（可选；不填的话访客需要在页面设置里自己填 Key）
4. 部署完成后拿到 `https://xxx.onrender.com`

说明：

- 免费实例 15 分钟无请求会休眠，首次访问冷启动约 30–60 秒
- 没有 torch / 权重，所以「本地 MiniMind」引擎不可用，默认走在线模型
- `data/` 在容器里，实例重启会丢会话与配置（免费方案限制）

## 方式 B：自己的服务器 / VPS（可跑本地模型）

```bash
git clone https://github.com/Sc30289/ai-chat.git
cd ai-chat
# 可选：把训练好的权重放到 out/ 或 ../minimind-vscode/out/
pip install torch transformers   # 只有本地引擎需要
python server.py --host 0.0.0.0 --port 8765
```

用 Nginx 反代 + HTTPS 后即可公网访问；记得不要把 `data/` 提交到仓库（里面有 API Key）。

## 方式 C：Docker

```bash
docker build -t ai-chat .
docker run -p 8765:8765 -e DEEPSEEK_API_KEY=sk-xxx ai-chat
```
