# RFQ Agent 接入飞书 · 实施方案

> 2026-11-04　**状态：设计稿，尚未实现**（仓库里没有 `rfq_agent/im/`，也没有相关测试）
> 前置调研：[RFQ_IM_INTEGRATION_ASSESSMENT.md](RFQ_IM_INTEGRATION_ASSESSMENT.md)

---

## 0. 这份文档是什么

一份**待建的实施方案**，不是已完成的工作记录。下面的模块名、表结构、函数签名都是"该长这样"，不是"已经长这样"。

落地节奏见 §12：D1 只做「长连接 + 一句回话」，D3 才到附件链路。每个里程碑自带验收标准，不需要一次做完。

设计里已经定死、后面不要轻易动的六件事：

| 决策 | 理由 |
|---|---|
| 事件 3 秒内只入队，不干活 | 飞书硬性超时，超了会重推 |
| 单 worker 串行 | 询价一天几单，串行可预测；瓶颈在排队而不是加副本 |
| 按钮参数自包含（task_id + revision + item_id + option_id） | 重启后旧卡还能点；审计能还原"他点的哪一版" |
| 卡片 1.0，不上流式 | MVP 不需要打字机；2.0 有客户端版本与 10 分钟关流约束 |
| 同进程引入，不开公网端口 | 现有服务无鉴权、默认绑回环 |
| 单实例部署 | 飞书长连接是随机投递一个 client，多副本必须有共享存储 |

---

## 1. 目标与边界

**要做成的**：销售在飞书群里把客户询价邮件（`.eml`）拖给机器人，30 秒内收到一张卡：解析到多少行、几行有价、哪几项要人拍板；点完按钮出 DOCX 草案；授权人点「批准入库」才落库。

**不做（本期）**：

1. 群文件/云文档互转——只发文件消息，不改写飞书文档；
2. 流式打字机——卡片 1.0 够用，2.0 + cardkit 留到二期；
3. 多副本高可用——单实例，理由见 §9.3；
4. 邮件自动收单——这是另一条链路，二期做；
5. 钉钉/企微——抽象已经留好（`contracts.py` 是唯一平台相关层），但不写不测。

---

## 2. 总体架构

```
飞书群（客户/销售/报价负责人）
   │  @机器人 或 发 .eml 附件
   ▼
┌──────────────────────────────────────────────────────────┐
│ rfq_agent/im/feishu.py      运输层                        │
│   lark-oapi 长连接（出网即可，无需公网 IP / 备案）          │
│   REST: 发消息 / PATCH 卡片 / 上传文件 / 下载附件           │
└───────────────┬──────────────────────────────────────────┘
                │ 原始事件 dict（3 秒内必须返回）
                ▼
┌──────────────────────────────────────────────────────────┐
│ contracts.parse()   →  Incoming(kind, chat_id, action…)   │
│ bot.accept()        →  校验白名单 + 入队 + 回 toast        │
└───────────────┬──────────────────────────────────────────┘
                │ SQLite job 表（去重键、重试计数）
                ▼
┌──────────────────────────────────────────────────────────┐
│ runner.Worker（单线程，串行）                              │
│   bot.execute(job) → 业务编排                             │
└───────────────┬──────────────────────────────────────────┘
                ▼
┌──────────────────────────────────────────────────────────┐
│ RFQService（现有确定性引擎，一行没改）                      │
│   understand → start_task → resolve_review → export → docx│
└───────────────┬──────────────────────────────────────────┘
                ▼
        cards.py 渲染 → PATCH 回同一张卡 / 发文件消息
```

三条时间线必须分开看，混在一起就是设计错误：

| 时间线 | 预算 | 谁负责 | 坏了会怎样 |
|---|---|---|---|
| 事件 ack | ≤3 s | `accept()` | 飞书重推 → 重复建任务（有去重兜底） |
| 一次业务动作 | 5–40 s | `execute()` | 卡片停在"处理中"，用户以为卡死 |
| 卡片更新 | ≥220 ms/次 | `update_card()` 节流 | 触发 230020 限频，卡片不刷新 |

---

## 3. 代码结构（计划新增，尚未创建）

```
rfq_agent/
  understand.py        模型优先 / 确定性兜底（从 web._understand 抽出，web 与 im 共用）
  im/
    __init__.py        分层说明
    contracts.py       飞书事件 JSON → Incoming；幂等键；/命令解析
    queue.py           SQLite 队列：入队、领取、重试上限、崩溃恢复
    cards.py           四种卡 + 30 KB 体积兜底 + toast
    sessions.py        每个群一条对话（Session 上下文落盘 + 裁剪）
    audit.py           谁在哪个群对哪单点了什么（只追加）
    bot.py             业务编排：accept() / execute()
    feishu.py          运输层：REST + 长连接
    runner.py          入口：service + worker + 长连接；--check
```

关键签名（实现时按这个写，接口不要散）：

```python
Incoming(kind, chat_id, message_id, user_id, text, file_key, file_name, action, event_id)
Incoming.dedupe_key() -> str

JobQueue.enqueue(kind, payload, chat_id, *, message_id, dedupe_key) -> int | None
JobQueue.claim() -> dict | None
JobQueue.fail(job_id, error) -> "queued" | "dead"
JobQueue.recover() -> int                     # 启动时把 running 放回队列

RFQBot.accept(incoming) -> dict               # 3 秒内返回，返回值就是卡片回调的 toast
RFQBot.execute(job) -> None                   # 单 worker 里执行

Transport: send_text / send_card / update_card / send_file / download
```

`Transport` 设计成 Protocol，是为了让"不接飞书也能测全链路"成为可能：测试时换成 `FakeTransport`，业务层代码一行不用改。

---

## 4. 数据模型（`runtime/im.db`）

```sql
job(id, kind, chat_id, message_id, dedupe_key UNIQUE, payload, status,
    attempts, error, created_at, started_at, finished_at)
    -- status: queued | running | done | dead

im_chat(chat_id PK, task_id, messages, updated_at)
    -- messages = Session.messages 的 JSON；重启后灌回新 Session

im_audit(id, at, chat_id, open_id, action, task_id, detail)
    -- 只追加；批准报价这条链靠它 + quote_header.approved_by 交叉验证
```

三张表都在同一个 SQLite 文件里，WAL 模式，各自一把锁。**没有改 `runtime/rfq.db` 一个字节**——任务仍然由现有 `TaskStore` 管，IM 层只是它的调用方和展示层。这是刻意的：接 IM 不该动定价引擎。

---

## 5. 事件契约

| 飞书事件 | 变成 | 关键字段 |
|---|---|---|
| `im.message.receive_v1` + `text` | `kind="text"` | 清掉 `@_user_N` 占位符；`/case B` 走命令分支 |
| `im.message.receive_v1` + `file` | `kind="file"` | `file_key` / `file_name`，只收 `.eml` |
| `card.action.trigger` | `kind="action"` | `action.value` 是自包含参数；`open_message_id` 用于 PATCH 回原卡 |

幂等键：

- 消息事件 → `msg:{message_id}`（飞书重推同一条消息时 message_id 不变）
- 卡片回调 → `act:{event_id}`（用户连点两次是两个不同 event_id，必须都处理；但同一个事件重投只处理一次）

`Incoming` 之外没有任何地方解析飞书 JSON，所以换平台只改一个文件。

---

## 6. 卡片规范

用**卡片 JSON 1.0**（`elements` + `tag: action`）。不上 2.0 的唯一原因：2.0 的流式能力要客户端 ≥7.20、单卡 10 次/秒、10 分钟自动关流，而 MVP 不需要打字机。

### 6.1 四种卡

| 卡 | 何时 | 按钮 |
|---|---|---|
| `progress_card` | 收到事件立刻 | 无（只表示"在跑"） |
| `preview_card` | 附件解析完、尚未入库 | 导入并开始处理 / 不是这封 |
| `summary_card` | 定价完成 | 逐条确认 / 生成草案 |
| `review_card` | 还有阻塞项 | 每项一组选项按钮 + 上一组/下一组 |
| `done_card` | 有产物 | 重新发送文件 / 批准入库 |

### 6.2 按钮参数自包含

```json
{"a": "resolve", "t": "<task_id>", "r": 3, "i": "RI-0019", "o": "quote_with_moq", "p": 0}
```

服务端不存"待处理的点击"。好处有三个：进程重启后旧卡片还能点；任务 renumbering 不会错位（`revision` 兜住）；审计能还原"他当时点的哪一版"。

### 6.3 30 KB 与分页

- 单卡硬上限 30 KB，`cards.ensure()` 在超限时按最长 markdown 逐次截断，截到 80 字还超就抛错（说明构建逻辑有 bug，不要静默发一张废卡）；
- 复核项一次 3 条，超出翻页（`page_size=3`），因为 122 行的单子可能有十几个待确认；
- 摘要卡只展示前 5 行 + "其余在网页端查看"，不做假全量。

---

## 7. 一次交互的完整时序

```
销售                飞书                 机器人进程                     引擎
 │  发 .eml           │                      │                          │
 ├───────────────────>│  事件（3s 预算）      │                          │
 │                    ├─────────────────────>│ accept: 白名单→入队→回进度卡 │
 │  ◀── "正在读取附件" ─┤                      │                          │
 │                    │                      │ worker: 下载附件          │
 │                    │                      ├─ stage_pending ──────────>│ 解析
 │  ◀── 预览卡（21 行）─┤<──── PATCH 同一张卡 ──┤                          │
 │  点「导入并开始处理」│                      │                          │
 ├───────────────────>│  卡片回调            │                          │
 │                    ├─────────────────────>│ 入队 run（同一张卡继续用） │
 │                    │                      │ understand → start_task  │
 │                    │                      ├─────────────────────────>│ 定价/合规
 │  ◀── 复核卡（2 项）──┤<─────────────────────┤                          │
 │  点「按 MOQ 报价」   │                      │                          │
 ├───────────────────>│                      │ resolve_reviewrevision+1 │
 │  ◀── 刷新（还剩 1 项）┤<─────────────────────┤                          │
 │  点「生成草案」      │                      │ export_quote_draft       │
 │  ◀── 完成卡 + .docx ─┤<──── 文件消息 ────────┤                          │
 │  点「批准入库」      │                      │ 校验授权人 → activate      │
 │                    │                      ├─ im_audit + quote_header>│
```

三个"卡住"的细节：

1. **同一张卡走到底**。附件来了先建进度卡，预览、解析、结果都 PATCH 这张卡，群里不会堆出一串消息；
2. **点旧卡片**（revision 过期）不报"失败"，而是"这条已经处理过（Stale revision…），卡片已按最新状态刷新"——同时把卡刷成最新，不然用户会一直对着过期内容点；
3. **模型挂了不等于流程挂了**。文字问答会退化成红卡"模型暂时不可用"，而发 `.eml`、点按钮的路径完全不经过模型。

---

## 8. 权限与身份

| 机制 | 实现 | 为什么需要 |
|---|---|---|
| 群白名单 | `FEISHU_ALLOWED_CHATS`，空=不限 | 建应用后先私聊调通，再放量到指定群 |
| 批准人白名单 | `FEISHU_APPROVERS`（open_id） | 「批准入库」是商业门禁，不能谁都能点 |
| 具名审批 | `activate_quote(approved_by=姓名(open_id))` | 引擎本来就拒绝 `system:*`；IM 身份正好把"谁批的"补成真人 |
| 审计 | `im_audit` 追加写 | 事后能答"这一版是谁在什么时间点的" |
| 失败关闭 | 没开 `RFQ_DEMO_APPROVAL` 时批准必失败 | 默认策略就是拒绝，演示才显式打开 |

`name_of(open_id)` 默认返回 open_id。要显示真名，接 `contact.v3.user.get`（需要通讯录权限），或者干脆用 `FEISHU_APPROVERS` 的映射表——后者不用申请权限，更适合试点。

---

## 9. 部署

### 9.1 配置（`.env`，和 GENESIS_* 放一起）

```text
FEISHU_APP_ID=cli_xxxxxxxx
FEISHU_APP_SECRET=xxxxxxxxxxxxxxxx
FEISHU_ALLOWED_CHATS=oc_xxxxxxxx          # 可留空，先私聊
FEISHU_APPROVERS=ou_xxxxxxxx              # 报价负责人
# FEISHU_BASE_URL=https://open.larksuite.com/open-apis   # 国际版才需要
```

### 9.2 启动（实现后）

```powershell
python -m rfq_agent.im.runner --check     # 1. 验凭证，不发消息
python -m rfq_agent.im.runner             # 2. 起 worker + 长连接
```

Linux 上用 systemd（关键点：`Restart=always`，长连接会断，SDK 自己重连，但进程挂了要拉起来）：

```ini
[Unit]
Description=RFQ Agent (Feishu)
After=network-online.target

[Service]
WorkingDirectory=/opt/rfq-agent
EnvironmentFile=/opt/rfq-agent/.env
ExecStart=/usr/bin/python3 -m rfq_agent.im.runner
Restart=always
RestartSec=5
# 只需要出网，不需要入站
[Install]
WantedBy=multi-user.target
```

安全组/防火墙：**不需要**开放任何入站端口。这是长连接方案最大的运维收益——不用买域名、不用备案、不用配证书、不用内网穿透。

### 9.3 为什么单实例（以及什么时候能扩）

飞书长连接的推送是**集群模式、不广播**：同一应用开 N 个 client，事件只随机给其中一个。而 `im.db`、`task.json`、`docx` 都在本地盘。多开会话不共享，会出现"这个群的历史在 A 副本、那张卡在 B 副本"。

要扩，顺序必须是：先把 `im.db` 换成共享库（Postgres/MySQL），任务目录换对象存储，再谈多副本；否则就是给自己造一个"点了没反应、重发一次又好了"的玄学 bug。

询价单量是一天几单，单 worker 的吞吐（每单 5–40 s）足够。真到瓶颈，先并行"只读查询"而不是加副本。

### 9.4 备份与日志

- `runtime/im.db`、`runtime/rfq.db`、`runtime/<task_id>/` 一起备份即可，都是普通文件；
- worker 每次 job 打一行 `[im] job N kind done in Xs`，失败打完整 traceback；
- 生产建议把 `print` 换成写文件，并加一条"dead 队列非空就告警"的巡检（`queue.stats()["dead"]`）。

---

## 10. 联调清单（飞书后台，按顺序做）

| # | 动作 | 验收 |
|---|---|---|
| 1 | 开放平台建**企业自建应用**，拿 App ID / Secret | `--check` 打印 "凭证可用" |
| 2 | 开「机器人」能力 | 应用详情里机器人开关为开 |
| 3 | 权限：`im:message`、`im:message:send_as_bot`、`im:resource` | 发送/上传不报 230027 |
| 4 | 事件订阅：添加 `im.message.receive_v1`，订阅方式选**长连接** | 保存时要求本地 client 在线 → 说明连接正常 |
| 5 | 回调订阅：添加 `card.action.trigger`，同样选长连接 | 点按钮有 toast |
| 6 | 版本管理与发布，可用范围加自己 | 单聊里能搜到机器人 |
| 7 | 把机器人拉进测试群 | 群里 @ 有响应 |
| 8 | 发一封 `.eml` | 3 秒内出进度卡，随后变预览卡 |
| 9 | 点「导入并开始处理」 | 同一张卡变复核/摘要 |
| 10 | 点选项 → 生成草案 | 收到 `.docx` 文件消息 |
| 11 | 非授权人点批准 | 红卡"没有批准权限" |

第 4、5 步有个反直觉的点：**保存订阅方式时要求长连接已经在线**。所以顺序是"先跑进程，再去后台点保存"，反过来会存不上。

---

## 11. 测试策略（实现时按这个分层）

| 层 | 文件 | 测什么 |
|---|---|---|
| 契约 | `test_im_contracts.py` | 字段映射、@ 清理、字符串 value、幂等键 |
| 可靠性 | `test_im_queue.py` | 去重、FIFO、重试上限、重启恢复、并发 claim 不重复 |
| 表达 | `test_im_cards.py` | 按钮参数完整、分页不漏项、30 KB 截断、越界页钳制 |
| 编排 | `test_im_flow.py` | 真 service 跑：附件预览与确认、复核推进 revision、旧卡点击、导出出文件、越权批准、模型缺失降级、非 eml 拒绝、重复事件只入队一次 |

编排层用 `FakeTransport` + 真 `RFQService` + 真 docx 渲染，所以它挂了基本等于"接上飞书也跑不通"。**这一层是这套设计里最值钱的资产**——它让"没有飞书凭证也能验证 90% 的逻辑"成立。

实现时第一个要盯的风险：`lark.JSON.marshal(obj)` 的字段形状随 SDK 版本变，而这一层测不到。对策是把原始 payload 原样落日志（一行 JSON），出问题能离线回放，而不是靠猜。

要加平台（钉钉）：复制 `feishu.py`，给 `contracts.py` 加一个 `parse_dingtalk()`，`bot.py` 一行不动。

---

## 12. 里程碑

| 天 | 交付 | 验收标准 |
|---|---|---|
| D1 | 建应用、权限、长连接跑通 echo | 群里 @ 机器人回一句话（用 `accept` 里最简分支） |
| D2 | 队列 + 幂等 + 文字问答 | 同一事件重投只处理一次；重启后队列能续跑 |
| D3 | 附件 → 预览 → 确认 → 摘要卡 | 案例 B 在群里 30 秒内出摘要，行数/可报价数与网页一致 |
| D4 | 复核卡 + 按钮回调 + revision | 点完 2 项后卡片不再显示待确认；旧卡点击有解释 |
| D5 | 导出 + 文件回传 + 批准门禁 | 群里收到 `draft-r*.docx`；非授权人批准被拒 |
| D6 | 部署 + 日志 + 重启演练 | 拔电重启，队列恢复，长连接自动重连 |
| D7 | 试点（1 个群、2–3 人） | 一周内至少 3 单真实邮件跑通，无误报价格 |

D1 之后每条都能独立演示，不必等全部做完。

---

## 13. 风险登记册

| 风险 | 概率 | 影响 | 对策（设计已定 / 需落实） |
|---|---|---|---|
| 事件处理超 3 秒被重推 | 高 | 重复建任务 | 设计：`accept` 只入队；`dedupe_key` 唯一约束 |
| 连点/旧卡点击 | 高 | 状态错乱 | 设计：revision 校验 + "已处理过"提示 + 刷新卡 |
| 卡片超 30 KB 发不出 | 中 | 大单不可用 | 设计：分页 + 出卡前体积校验收口到一处 |
| 卡片更新触发 5 QPS | 中 | 卡片不刷新 | 设计：单卡 ≥220 ms 节流 |
| 模型超时/未配置 | 中 | 文字问答不可用 | 设计：降级红卡；确定性链路不依赖模型 |
| 明文手机号/邮箱触发内容审查(230028) | 中 | 消息被拦 | 待办：出卡前脱敏（客户联系人只显示姓名） |
| `lark-oapi` 版本升级改了 `marshal` 字段 | 中 | 事件解析失败 | 设计：所有解析集中在 `contracts.parse`；待办：落原始 payload，便于离线回放 |
| 批准越权 | 低 | 合规事故 | 设计：审批人白名单 + `im_audit` + 引擎自身拒绝 `system:*` |
| 报价数据出网 | 低 | 合规问询 | 待定：模型 API 域内（`token.moi.matrixorigin.cn`），数据留在 `runtime/` 本地 |
| 单实例成为瓶颈 | 低 | 排队变长 | 已接受：询价单量低；扩容路径见 §9.3 |

---

## 14. 二期（按价值排序）

1. **邮件自动收单**：IMAP/Exchange 定时拉取 → 自动起任务 → 群里推"新询价，3 项待确认"。这一步比继续打磨 IM 更能提升真实使用率，因为入口在邮箱，不在群。
2. **流式卡片**（JSON 2.0 + cardkit）：首帧出"正在解析第 12/21 行"，体验更像 AI；注意 10 分钟自动关流、流式期间不能响应回调。
3. **审批单打通**：`activate_quote` 前发飞书审批实例，批准回写 `approved_by`，把"群里点按钮"升级成"有审批编号的批准"。
4. **多副本**：先换共享存储，再谈横向扩（§9.3）。
5. **钉钉**：`contracts.parse_dingtalk()` + `transport_dingtalk.py`，业务层零改动；工作量约 2–3 人日，主要在卡片模板（钉钉要后台建模板）。

---

## 15. 一页速查

```text
入口（待建）  python -m rfq_agent.im.runner [--check]
配置          .env: FEISHU_APP_ID/SECRET/ALLOWED_CHATS/APPROVERS
状态（待建）  runtime/im.db（job/im_chat/im_audit）+ runtime/<task_id>/
不变量        3 秒内 ack；单 worker 串行；按钮参数自包含；卡片 ≤30KB；批准要具名
失败姿态      模型挂了→红卡；飞书挂了→队列积压；进程挂了→重启续跑；重复事件→去重
不做什么      多副本、流式、云文档、邮件收单（二期）
```