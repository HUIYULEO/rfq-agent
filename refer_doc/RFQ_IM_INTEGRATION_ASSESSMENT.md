# RFQ Agent 接入飞书 / 钉钉 / 企业微信 可行性评估

> 结论日期：2026-11-03　评估对象：本仓库 `rfq_agent`（`python -m rfq_agent.web` 那一套）
> 问题：把这个智能体接进飞书或别的国内办公软件，有多难？

---

## 0. 一句话结论

**协议接入不难，难的是把"单机单进程的演示服务"改成"多用户常驻服务"，以及真正的业务入口（客户邮件）根本不在 IM 里。**

| 目标 | 难度 | 实际工作量（1 名熟悉 Python 的后端） |
|---|---|---|
| 飞书群里能 @ 机器人、发 `.eml`、收回一份 DOCX 草案 | ★★☆☆☆ | **4–6 人日**（含建应用、卡片、异步、部署） |
| 飞书内部试点：多群、多用户、重启不丢、能审计 | ★★★☆☆ | **+5–8 人日** |
| 飞书生产可用：权限、幂等、限流、监控、审批留痕 | ★★★★☆ | **累计 4–6 周** |
| 换钉钉（Stream 模式） | 比飞书 **+30%~50%** | 同一套适配层重写 |
| 换企业微信（智能机器人） | 比飞书 **+50%~100%**，且输出能力受限 | 见 §2.3 |
| 用扣子 / 百炼 / 元器 重搭 | ★★★★★（要重写编排） | 不建议；本项目的价值在确定性定价引擎，不在编排 |

关键事实：**飞书、钉钉、企业微信三家现在都有官方 WebSocket「长连接」通道，不需要公网 IP、不需要域名备案、不需要内网穿透。** 这一步过去是整个接入里最劝退的门槛，现在基本消失了。

---

## 1. 先看清"我们这个 agent 的接入面"

现有实现（读代码确认，不是猜）：

| 事实 | 位置 | 对 IM 接入的含义 |
|---|---|---|
| 纯 stdlib 的 HTTP JSON 服务，`ThreadingHTTPServer`，默认绑 `127.0.0.1:8765` | `rfq_agent/web.py:577` | 改造成本极低；但没有鉴权、没有 HTTPS、没有 OpenAPI 描述 |
| 会话状态在进程内存：`SESSIONS: dict[str, Session]` | `web.py:38` | **重启即丢**；多副本之间不共享，这是接 IM 后第一个会踩的坑 |
| 全局 `LOCK = threading.Lock()`，且 `/api/run` 在锁内调用模型 | `web.py:36, 489-520` | **一次询价解析（含模型调用 10s+）会把所有其他请求串起来**；IM 场景天然并发，这里必须改 |
| 任务状态落盘：`runtime/<task_id>/task.json` | `storage.py` | 好消息：任务本身是持久化的，重启不丢；丢的只是"聊天上下文" |
| 复核项结构天然适合做卡片按钮：`{id, question, options:[{id,label}], evidence, severity}` | `pricing.py:539-607` | **这是接 IM 最省事的地方**——一张卡片一组按钮，点一下就是 `POST /api/resolve` |
| 产物是 `.docx` 文件 | `runtime/<task_id>/*.docx` | 飞书/钉钉/企微都支持"发文件消息"，30 MB 以内随便传（我们的 docx 只有几十 KB） |
| 模型层是 OpenAI 兼容 + 工具循环，10 个工具 | `genesis.py:144-235` | 与 IM 无关，不用动 |
| 单进程、单实例设计 | `README.md` §还没做 第 3 条 | 产品化最大的债，不是 IM 带来的 |

一句话：**这个仓库已经长出了"接 IM 所需要的 80% 语义"（任务、复核项、选项、产物、审计日志），缺的是"IM 所需要的那层壳"和"多用户所需的并发模型"。**

---

## 2. 三家平台的硬约束（都会直接影响设计）

### 2.1 飞书（推荐）

| 约束 | 具体值 | 影响 |
|---|---|---|
| 长连接 | 官方 SDK `lark-oapi`，`lark.ws.Client(app_id, app_secret).start()`；仅企业自建应用；**服务器只要能出网** | 笔记本上就能跑，演示极友好 |
| 响应时限 | 长连接收事件 **3 秒内**处理完且不抛异常，否则触发重推 | 解析+定价要 10–30s → **必须"先回执、后异步"** |
| 多副本 | 长连接是**集群模式，不广播**：同一应用部署多个 client，事件只随机投给其中一个 | 想水平扩容，就必须把会话/任务状态挪到共享存储 |
| 卡片更新 | 单条消息 **5 QPS**、发送后 **14 天**内可改、卡片体 **≤30 KB**、必须 `update_multi: true` | 122 行的表塞不进一张卡 → 只能"摘要 + 前 N 行 + 链接" |
| 流式卡片 | JSON 2.0、客户端 ≥7.20、单卡 **10 次/秒**、10 分钟自动关流 | 想要"打字机"效果才需要，MVP 可以不做 |
| 文件 | 上传 **≤30 MB**，`.docx` 用 `file_type=stream` | 够用 |
| 卡片回调 | `card.action.trigger` **支持长连接**（旧版"消息卡片回传交互"不支持） | 按钮流可以全程免公网 |
| 内容审查 | 消息含明文手机号/邮箱可能触发数据防泄漏审查（错误码 230028） | 报价里若带客户联系人，可能被拦，需清洗 |

来源：[长连接接收事件](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/event-subscription-guide/long-connection-mode)、[长连接接收回调](https://open.feishu.cn/document/event-subscription-guide/callback-subscription/step-1-choose-a-subscription-mode/configure-callback-request-address)、[事件回调优化（3 秒）](https://open.feishu.cn/document/event-subscription-guide/event-subscriptions/event-callback-optimization-guide)、[更新消息卡片（5 QPS/14 天/30KB）](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/patch)、[流式更新卡片](https://open.feishu.cn/document/cardkit-v1/streaming-updates-openapi-overview)、[上传文件（30MB）](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/file/create)。

### 2.2 钉钉

- **Stream 模式**同样是官方 WebSocket，免公网 IP、免域名备案，企业机器人 + 互动卡片回调都在上面跑（[官方说明](https://help.dingtalk.io/open/development/introduction-to-stream-mode)）。
- 卡片要在后台用模板搭，回调参数与飞书不同；群自定义机器人 webhook 只能单向推送（约 20 条/分钟）。
- 净差异：**能力对等，但 SDK 成熟度、文档连贯性、Python 示例质量都弱于飞书**，踩坑时间约多 30%~50%。

### 2.3 企业微信（最不推荐给这个场景）

- 新版**智能机器人**确实有 WebSocket 长连接（免公网），比老的自建应用回调（必须公网 URL，且历史上只认 80/443）好很多。
- 但输出侧限制明显（第三方实践文档梳理得很清楚，见 [OpenClaw-WeCom-Streaming-Architecture](https://github.com/partme-ai/openclaw-plugins/blob/82367a161529631b2743e5cc3a5d571934c2a86d/doc/wecom/OpenClaw-WeCom-Streaming-Architecture.md)）：
  - `replyStream` **只支持纯文本，不支持 Markdown**；
  - 流窗口 **6 分钟**，超时错误码 846608；
  - 单条流内容截断 **20 KB**；
  - 群文件能力受 Bot 限制，媒体往往要退回"私信 Agent 发送"；
  - 智能机器人 Bot 支持流式，普通 Agent 自建应用**不支持**。
- 对我们这种"表格 + 复核按钮 + 附件回传"的产品形态，等于把最擅长的表达方式全砍掉。
- 另外，企微的自建应用/可信域名/可信 IP 往往还牵扯企业认证，行政成本高于飞书。

---

## 3. 真正的工作量在哪：一张改造清单

按"最小可用（Demo）→ 试点 → 生产"三档拆。

### P0 · 适配层（新增代码，不动现有逻辑）

| # | 工作项 | 说明 | 人日 |
|---|---|---|---|
| 1 | 建飞书自建应用、开权限、开机器人、发布 | 需要租户管理员；可用范围要配，否则报 230013 | 0.5 |
| 2 | 新增 `rfq_agent/im/feishu_bot.py`：`lark-oapi` 长连接 + 事件分发 | 文本消息 → 复用 `Session.send()`；一张卡片模板 | 1.0 |
| 3 | 接收文件消息（`.eml` / `.xlsx` / `.pdf`）→ `stage_pending` + `confirm_pending` | 复用 `POST /api/preview`、`/api/upload` 的既有逻辑 | 0.5 |
| 4 | 结果卡片：摘要（客户/单号/行数/可报价数/待确认数）+ 复核项按钮 + 产物链接 | 复核项结构现成；注意 30 KB 与 122 行的取舍 | 1.5 |
| 5 | `card.action.trigger` → `resolve_review` / `export_draft` / `activate_quote` | 回调 value 里带 `{task_id, revision, item_id, option_id}` | 1.0 |
| 6 | 异步执行 + 进度回写 + 幂等 | 3 秒 ack；用 `event_id` 去重；卡片更新 5 QPS 限速 | 1.0 |
| 7 | 产物回传：上传 docx → `file_key` → 发文件消息 | 30 MB 限制无压力 | 0.5 |
| 8 | 部署：Linux + systemd/docker，出网即可；日志与自动重连 | 不能用笔记本当长期运行环境 | 0.5 |

**合计 ≈ 6.5 人日**（其中 1、8 可以并行；若只做单群演示可压到 4 天）。

### P1 · 试点级（不改业务语义，只补工程债）

| 工作项 | 为什么必须 | 人日 |
|---|---|---|
| 会话持久化：`SESSIONS` → SQLite 表（chat_id → 历史/当前 task） | 现在重启即丢；IM 用户默认"你记得我上一句" | 1.0 |
| 并发模型：把 `_run` 里"锁内调模型"的写法改成"任务队列 + 单 worker" | 现状是**一个人解析时所有人都卡住** | 1.5 |
| 多群/多用户隔离 + 一个群 = 一个工作区 | `inbox` 现在是全局的，两个人同时导素材会互相干扰 | 1.0 |
| 失败兜底：模型超时、文件解析失败、卡片超限降级为文本 | 群里的失败必须"说人话"，不能静默 | 1.0 |
| 审计：谁点了哪个按钮（open_id + 姓名）写进 `decision_log` / ledger | `activate_quote` 本来就要求具名的人，IM 身份正好补上这一环 | 0.5 |

**合计 ≈ 5 人日**

### P2 · 生产级

| 工作项 | 说明 | 人日 |
|---|---|---|
| 权限模型 | 谁能发询价、谁能复核、谁能批准；可用范围 + 角色映射 | 2 |
| 幂等与重放 | 飞书会重推事件；卡片可能被连点 | 1 |
| 可观测 | 结构化日志、任务耗时、失败率、告警到运维群 | 2 |
| 数据合规 | 报价数据落盘位置、模型 API 是否出网、脱敏（明文手机号/邮箱会触发 230028） | 2 |
| 压测与容量 | 单 worker 串行能扛几单/天；122 行大单的内存与耗时 | 1 |
| 邮件自动收单（IMAP/Exchange） | **见 §4，这才是业务闭环的关键** | 3 |

**合计 ≈ 11 人日**，与 P0/P1 叠加约 **4–6 周**（含联调与返工）。

---

## 4. 三个容易被低估的坑

### 坑 1：飞书不是业务入口，邮箱才是

现在的素材来源是 `data/询价信息素材/**/*.eml`（`cases.py` 扫目录）。真实的询价是**客户发到公司邮箱**。

- 接 IM 只解决了"人机界面"，没解决"单据从哪来"；
- 如果只做 IM，业务员每天还是要手动把邮件转发进群 → 省下的时间有限；
- 真正值钱的是 **IMAP/Exchange 收单 → 自动起任务 → 群里推送"这单有 3 个待确认"**。

建议把邮件接入和 IM 接入**一起立项**：邮件是入口，IM 是通知与复核台。只做 IM 的 ROI 会被高估。

### 坑 2：3 秒 vs 30 秒，逼着整个链路异步化

飞书要求 3 秒内 ack，而：

- 感知层（模型抽表）单次调用 10s 量级；
- 定价 + 生成 docx 再 1–3s；
- triage（模型审结果）超时上限 90s。

所以架构上必然变成：**收到消息 → 立即回"已收到，正在解析"卡片（3s 内）→ 后台线程跑 → PATCH 同一张卡**。这不是"优化"，是硬要求。好处是顺手把"进度可见"做出来了，体验比 Web 页面还好。

### 坑 3：并发与"单进程"假设的对撞

现状：

- `LOCK` 是进程内锁，覆盖了 `/api/run`（含模型调用）与 `/api/export`；
- `SESSIONS` 是进程内字典；
- `task.json` 落在本地盘；
- README 明说"多用户并发（当前存储只支持单进程顺序调用）"未做。

IM 一接进来，"并发"从"可能"变成"默认"——两个人同时在群里问，就会排在同一个锁后面。而飞书长连接的多副本投递又是随机的，想靠"多开几个实例"绕开也不行，除非先把状态挪到共享存储（SQLite 单写者 / Postgres / Redis）。

**结论：IM 接入本身是 1 周的事，但它会把"单机演示"升级成"常驻服务"，那部分才是 80% 的工作量。**

---

## 5. 一个务实的最小方案（建议照这个做）

```
飞书群 / 单聊
   │  @机器人  或  直接发 .eml 附件
   ▼
[im/feishu_bot.py]  lark-oapi 长连接（无需公网）
   ├─ im.message.receive_v1     → 文本 / 文件
   ├─ card.action.trigger       → 点按钮
   └─ 3s 内 return，投递到 JobQueue
   ▼
[JobQueue]  SQLite 表 + 单 worker 线程（替代现在的全局 LOCK）
   ▼
[RFQService] 现有确定性引擎（解析 → 定价 → 复核门 → docx）
   ▼
[卡片渲染] 摘要卡（≤30KB）+ 复核按钮 + docx 文件消息
```

**为什么要单 worker**：询价单量本来就是"一天几单"，串行完全够；但串行必须是"排队"而不是"占着全局锁"，这样"查询/看历史"这类只读操作不会被长任务卡死。

**进程内还是旁路进程**：建议**同进程引入**（adapter 直接 `from .service import RFQService`，共用 `LOCK`/store），而不是另起一个进程去调 `127.0.0.1:8765` 的 HTTP。理由：现有服务没有鉴权、默认只绑回环、`_view()` 依赖进程内全局 `SERVICE`；旁路方案为了让它能对外，反而要额外补鉴权和 HTTPS。代价是 `lark-oapi` 多一个依赖，可放进 `pyproject.toml` 的可选组（`[project.optional-dependencies] im = ["lark-oapi>=1.4"]`），保持"没装也能跑 Web 演示"。

**为什么先做飞书**：长连接 + 卡片 + 文件三件事都是官方一等公民，且 `card.action.trigger` 支持长连接，意味着**整条链路可以跑在一台没有公网 IP 的内网机器上**，演示和试点都不用买域名。

**演示脚本（5 分钟）**：群里发 `.eml` → 卡片 3 秒内出现"已收到，正在解析" → 15 秒后卡片变成摘要（21 行 / 19 有价 / 2 待确认）→ 点"按 MOQ 报价" → 卡片刷新 → 点"生成草案" → 群里收到 `.docx`。

---

## 6. 难度打分（1=容易，5=难）

| 维度 | 飞书 | 钉钉 | 企业微信 |
|---|:--:|:--:|:--:|
| 拿凭证、建应用 | 2 | 2 | 3（认证/可信域名） |
| 收发消息（长连接） | 1 | 2 | 2 |
| 富交互卡片 | 2 | 3 | 4（纯文本流、能力受限） |
| 收文件 / 发文件 | 1 | 2 | 3（群文件受限） |
| 与现有 HTTP 服务对接 | 1 | 1 | 1 |
| 多用户/多群改造 | 4 | 4 | 4（三家同样） |
| 审批/身份留痕 | 2 | 2 | 2 |
| **综合** | **2.0** | **2.6** | **3.3** |

---

## 7. 不建议的几条路

1. **用扣子/百炼/元器重搭**：这些平台擅长"提示词 + 插件"，但我们的核心资产是 6.5k 行 Python（含 672 行确定性定价、合规门、审计链、121 个测试）；搬到 DSL 里等于把可验证性换成不可验证性。
2. **把 agent 做成 MCP server 让平台调用**：方向对（飞书有 lark-mcp），但当前平台侧"AI 助手调用外部 MCP"仍在早期，链路不稳，且解决不了"群里发文件"这个主场景。
3. **只做群自定义机器人 webhook 单向推送**：0.5 天能出效果，但没有交互、没有复核按钮，等于把最关键的 HITL 环节砍了。

---

## 8. 建议的推进顺序

1. **本周**：飞书自建应用 + 长连接 + 文本问答（1 天），先验证"群里能用"这件事；
2. **下周**：文件消息 → 任务 → 摘要卡片（2 天），做出可演示的闭环；
3. **再下周**：复核按钮 + docx 回传 + 异步化（2 天），此时已经是"内部可用"；
4. **然后**：把邮件接入（IMAP）排进来——它比继续打磨 IM 更能提升真实使用率。

如果只能投 3 天，就做 1 + 2 的前半段，换来一个可以给评委/老板看的"飞书里发邮件，30 秒出草案"的演示；剩下的工程债留到有真实用户再说。

---

## 9. 参考

- 飞书 [使用长连接接收事件](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/event-subscription-guide/long-connection-mode) / [使用长连接接收回调](https://open.feishu.cn/document/event-subscription-guide/callback-subscription/step-1-choose-a-subscription-mode/configure-callback-request-address)
- 飞书 [事件回调优化指南（3 秒超时与重试）](https://open.feishu.cn/document/event-subscription-guide/event-subscriptions/event-callback-optimization-guide)
- 飞书 [更新已发送的消息卡片（5 QPS / 14 天 / 30KB）](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/patch)
- 飞书 [流式更新卡片](https://open.feishu.cn/document/cardkit-v1/streaming-updates-openapi-overview)
- 飞书 [上传文件（30MB）](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/file/create)
- 钉钉 [Server-Side Stream Mode](https://help.dingtalk.io/open/development/introduction-to-stream-mode) / [配置 Stream 推送](https://help.dingtalk.io/zh/open/dingstart/configure-stream-push)
- 企业微信 [智能机器人长连接](https://developer.work.weixin.qq.com/document/path/101463)
- 第三方实践（企微流式的硬约束梳理）：[OpenClaw-WeCom-Streaming-Architecture](https://github.com/partme-ai/openclaw-plugins/blob/82367a161529631b2743e5cc3a5d571934c2a86d/doc/wecom/OpenClaw-WeCom-Streaming-Architecture.md)