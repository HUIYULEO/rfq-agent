# 部署到公网（评委链接）

> **当前线上实例：** https://rfq-agent-xk2m.onrender.com/ （Render 免费版，新加坡区，2026-11-10 公网实测全流程通过）。
> 提交用的链接以 `RFQ_SUBMISSION.md` §0 为准；本文记录如何复现这套部署，以及在评审期间保持可用的做法。

目标：按赛事 §13.1 提供一个**未登录浏览器可直接打开**的链接，评委能选案例、看结构化提取、
看依据、下载 DOCX 草案；评审期间服务保持可用。

本文只写已验证的事实和可执行步骤。凡是"需要在你的账号里点"的步骤，都写清楚点哪里。

---

## 1. 当前状态与三个真实阻塞项

代码本身已经可以对外提供服务（本机用干净目录跑通了完整链路，见第 5 节）。真正拦住公网访问的是三件事：

| # | 阻塞项 | 事实 | 处理 |
|---|---|---|---|
| 1 | **素材没进 Git** | `git ls-files data` 为空：`data/询价信息素材` 只在本地。从 GitHub 部署出来是**空收件箱**，评委看不到任何案例 | 把 `data/` 提交进仓库（本文默认已做） |
| 2 | **只监听回环** | `web.py` 原来默认 `--host 127.0.0.1`，且不认 `$PORT`，多数平台注入端口后会连不上 | 已改：`RFQ_HOST` / `PORT` 环境变量，默认值仍是本地 8765 |
| 3 | **没有健康检查端点** | 平台无法判断实例是否真的可用 | 已加 `GET /healthz`，只读、不碰存储锁 |

已顺手修掉的两个运行时隐患：

- `Task.from_dict` 现在会忽略旧快照里的废弃字段（例如已删除的 `triage`）。此前只要
  `runtime/` 里留着旧 `task.json`，`/api/fixtures` 就整条 500——评委看到的正是这条路径。
- 进程收到 `SIGTERM`（容器停止）时会正常关闭，不再靠强杀。

## 2. 环境变量

| 变量 | 必填 | 作用 | 不配的后果 |
|---|---|---|---|
| `PORT` | 平台注入 | 监听端口 | 落到默认 8765，平台探测失败 |
| `RFQ_HOST` | 建议 `0.0.0.0` | 绑定地址 | 默认回环，外部访问不到 |
| `GENESIS_BASE_URL` | 可选 | 模型网关 | 对话层禁用，按钮流程照常 |
| `GENESIS_MODEL` | 可选 | 模型 ID，如 `deepseek-v4.1-flash` | 同上 |
| `GENESIS_ACCESS_TOKEN` | 可选 | 访问令牌（**只放服务端**，不进仓库、不进页面） | 同上 |
| `RFQ_DEMO_APPROVAL` | 可选 `1` | 打开"批准入库"演示；产物标注「演示批准」 | 默认策略拒绝激活，只到草案 |

模型层是**可选增强**：没配 token 时页面右侧会提示缺哪两个值，六步按钮流程不受影响。
赛事要求"链接中无 API Key"，所以令牌只以环境变量形式存在，不写进任何前端可见的地方。

## 3. 三条路线怎么选

| 维度 | Render 免费版 | Zeabur | 自有 VPS（阿里云/腾讯云轻量） |
|---|---|---|---|
| 免费？ | 是，不需信用卡 | 免费版只**托管你自己的服务器**，共享算力要 Dev（$5/月起，14 天试用） | 否，约 ¥40–100/年（促销价） |
| 会不会休眠 | 15 分钟无访问休眠，下次访问冷启动 | 不休眠（按你的服务器） | 不休眠 |
| 国内访问 | 一般（新加坡区约 100–200ms，偶有波动） | 较快（港/新/日节点） | 最快 |
| 部署方式 | 连 GitHub，认 Dockerfile，一键 | 连 GitHub，认 Dockerfile | `docker compose up` 或 systemd |
| 评审期稳定性 | 需一个定时 ping 保持唤醒（免费额度 750 小时/月，够一个服务跑满） | 取决于你的机器 | 取决于你的机器 |
| 备案 | 不需要（域名不在大陆） | 不需要 | 用 IP:端口不需要备案；绑域名走 80/443 才需要 |

**建议**：先上 Render 免费版（0 成本、一次配置），正文和视频链接照常给。如果担心评委所在网络
到海外不稳，再补一台轻量云服务器（¥100/年量级）用第 4.3 节的脚本兜底——两条链接可以同时存在，
提交时填主链接，文档里注明备用。

Zeabur 的免费版并不提供免费运行算力（它的 Free 计划是"管理 1 台你自己买的服务器"），
要它托管就得订阅 Dev；所以如果本意是"零成本"，Render 比 Zeabur 更直接。

## 4. 具体操作

### 4.1 Render（推荐，Docker 一键）

前提：仓库是 **private**，第一次要授权 Render 读这个仓库（Dashboard → Account Settings →
Git Provider → 选 `HUIYULEO/rfq-agent`）。

1. 打开 <https://dashboard.render.com/blueprints> → **New Blueprint Instance**；
2. 选仓库 `HUIYULEO/rfq-agent`，分支 `main`；
3. Render 读到根目录的 `render.yaml`，按它建一个 free web service（新加坡区、Dockerfile 构建、
   健康检查 `/healthz`）；
4. 建服务时它会要求填 `GENESIS_ACCESS_TOKEN`（`sync: false` 的变量），粘贴你的令牌；
   不填也能起，只是对话层禁用；
5. 等构建（首次约 3–5 分钟），拿到 `https://rfq-agent-xxxx.onrender.com`。

构建完先验证：

```bash
curl -s https://<你的地址>/healthz
# {"ok":true,"service":"rfq-agent","materials":true,"runtime_writable":true}
```

`materials:false` 说明 `data/` 没进镜像，别急着提交链接。

保持唤醒：在 UptimeRobot（免费）建一个 HTTP 监控，地址 `https://<你的地址>/healthz`，
间隔 5 分钟。免费版实例小时数按"运行中的实例"计，一个服务 24/7 约 730 小时/月，在 750 以内。

### 4.2 Zeabur

1. Dashboard → Create Project → 选区（香港/新加坡）；
2. Add Service → GitHub → 选 `rfq-agent`，Zeabur 会自动识别根目录 `Dockerfile`；
3. 环境变量照第 2 节填（`RFQ_HOST=0.0.0.0` 已在 Dockerfile 里，`PORT` 由 Zeabur 注入）；
4. Networking → Generate Domain，得到 `xxx.zeabur.app`。

### 4.3 自有 VPS（最稳）

```bash
# 服务器上（Ubuntu 22.04+，已装 Docker）
git clone https://github.com/HUIYULEO/rfq-agent.git && cd rfq-agent
docker build -t rfq-agent .
docker run -d --name rfq --restart unless-stopped \
  -p 127.0.0.1:8000:8000 \
  -e GENESIS_BASE_URL=https://token.moi.matrixorigin.cn/v1 \
  -e GENESIS_MODEL=deepseek-v4.1-flash \
  -e GENESIS_ACCESS_TOKEN='<你的 token>' \
  -e RFQ_DEMO_APPROVAL=1 \
  rfq-agent
```

前面用 Caddy 拿 HTTPS（把 `demo.example.com` 换成你的域名，A 记录指到这台机器）：

```caddyfile
demo.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

没有域名就直接用 `http://<公网IP>:8000`（用 8000 这类非 80/443 端口不需要备案）。
安全组放行对应端口即可。

注意：**不要开多进程/多 worker**。存储是单进程 SQLite + 进程内锁，多副本会互相看不到状态。

## 5. 部署前应在本机验证的（已完成一次）

用不含 `.git`、`runtime`、`.deps` 的干净目录模拟"从仓库拉下来"，实测结果：

| 步骤 | 结果 |
|---|---|
| `python -m rfq_agent.web` 启动 | 正常，`/`、`/api/cases`、`/api/data` 均 200 |
| `POST /api/import {"key":"B"}` | 21 行入库 |
| `POST /api/run {"case":"B"}` | `awaiting_review`，2 个待确认项（RI-0016 / RI-0019），19 行有价 |
| `POST /api/resolve` ×2 | 第二次后 `disposition=QUOTE`、`quote_allowed=true`、revision 3 |
| `POST /api/export` | 产出 `draft-r3.json` / `analysis-r3.docx` / `quote-draft-r3.docx` |
| `GET /api/download` ×3 | 200，358 KB / 38.7 KB / 14.6 KB |

评委视角只需要：打开链接 → 左侧选客户 B → 开始处理 → 答两个问题 → 导出草案 → 下载。

## 6. 提交前自检（对照 §13.4）

- [ ] 用**未登录浏览器**（或无痕窗口）打开链接，页面完整、能跑完 B 案例、能下 DOCX
- [ ] 链接里没有 token、账号、个人信息；`REF` 页面不显示 `.env`
- [ ] `/healthz` 返回 `materials:true`
- [ ] 冷启动可接受：休眠后第一次点击约 30–60 秒，页面不报错
- [ ] 已配置保活监控（若用 Render 免费版）
- [ ] 说明文档第 5.1 节填的是**真实链接**，不是占位符
- [ ] 视频里录一遍同样的流程，作为在线服务故障时的兜底证据

## 7. 已知限制（写进说明文档，别等评委问）

| 限制 | 说明 |
|---|---|
| 冷启动 | 免费实例休眠后首次访问 30–60 秒；保活监控可消除 |
| 单进程 | 存储为单进程 SQLite；并发演示会串行，不适合多用户压测 |
| 无鉴权 | 任何拿到链接的人都能重置演示数据、上传 `.eml`；赛事要求免登录，故不加登录墙 |
| 上传落盘 | 用户上传的 `.eml` 写到实例内 `runtime/uploads/`，平台重建即清空，不跨实例同步 |
| 数据持久 | 免费计划无持久盘；重启后收件箱回到"只有案例 B"的初始态，这正是演示想要的 |
| 模型费用 | 对话层走 Genesis，按调用计费；评审期建议保留额度或关闭该层 |
