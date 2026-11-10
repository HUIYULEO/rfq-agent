# MatrixNEXT RFQ Agent

面向航空配件与航材贸易的询价（RFQ）处理智能体：客户邮件进，**可复核、可追溯**的报价草案出。

它明确拒绝两件事：编造没有价格依据的数字，以及在无人决定的情况下把草案变成正式报价。

| | |
|---|---|
| 语言 | Python 3.11+ |
| 运行时依赖 | 仅 `openpyxl`（读 Excel）、`python-docx`（写 Word）；HTTP、SQLite、PDF 解析均为标准库 |
| 服务形态 | `python -m rfq_agent.web`，无 Web 框架、无构建步骤 |
| 测试 | 180 个用例，`python -m unittest discover -s tests` |

---

## 目录

- [解决的问题](#解决的问题)
- [快速开始](#快速开始)
- [处理流程](#处理流程)
- [设计约束](#设计约束)
- [解析与理解](#解析与理解)
- [合规与业务规则](#合规与业务规则)
- [人工复核门](#人工复核门)
- [对话层（可选）](#对话层可选)
- [报告与导出](#报告与导出)
- [数据层](#数据层)
- [配置](#配置)
- [HTTP 接口](#http-接口)
- [测试](#测试)
- [示例数据](#示例数据)
- [目录结构](#目录结构)
- [已知限制](#已知限制)
- [相关文档](#相关文档)

## 解决的问题

一封客户询价邮件里同时包含：散落在正文或附件里的行项目、编号写的商务条款、以及「CERT REQUIRED」这类一票否决的产品要求。人工处理时，真正耗时的不是算价，而是**确认每个料号、数量、单位、证书要求的原文到底在哪一行**，以及**判断哪些单子根本没法报**。

本系统把这段工作拆成可审计的步骤：

- 从邮件原文解析行项目与条款，每个字段带回指原文的定位（邮件 → 附件 → 表/页 → 行 → 列 → 原值）；
- 用确定性的定价引擎在参考价格表与内部成本表里选源、选价、算金额；
- 逐行逐规则核查件号匹配、证书、单位、件状与报价截止；
- 把所有需要人拍板的事情收成一个待确认项列表，答完才继续；
- 每单都产出询价分析报告；只有具备报价条件且经**具名人工批准**的单子，才生成正式报价并入库。

## 快速开始

```powershell
pip install -e ".[documents]"
python -m rfq_agent.web --port 8765
# 打开 http://127.0.0.1:8765/
```

Windows 下也可直接双击 `start.cmd`：检查 Python 版本与依赖、缺依赖时自动安装、拉起浏览器。

命令行等价入口：

```powershell
python -m rfq_agent.cli case --case B                    # 解析 + 定价 + 给出待确认项
python -m rfq_agent.cli case --case B --resolve RI-0016=quote_with_moq --export
python -m rfq_agent.cli case --case B --autonomy auto_draft --export
python -m unittest discover -s tests -v
```

容器：

```powershell
docker build -t rfq-agent .
docker run -p 8000:8000 -e GENESIS_ACCESS_TOKEN=<token> rfq-agent
```

`GET /healthz` 返回服务状态与两项部署关键条件（素材目录是否存在、`runtime/` 是否可写），供平台探针使用。

## 处理流程

```text
.eml（正文表格 / 附件 xlsx / 附件 pdf）
      │
      ▼
[1] 解析      四种载体归一到同一套列名，每行带 evidence 定位
      │        （可选：模型先读懂邮件，代码再逐值核对锚点与置信度）
      ▼
[2] 定价      确定性引擎在价格表/成本表里选源、选价、算金额，纯函数
      │
      ▼
[3] 核查      evaluation（契约自检）+ business_rules（逐行逐规则）
      │
      ▼
[4] 复核门    有阻塞项 → awaiting_human，任务停在原处等待回答
      │        无阻塞项 → draft_ready
      ▼
[5] 导出      analysis-r{n}.docx 每单必出；可报价时另出 quote-draft-r{n}.docx
      │
      ▼
[6] 批准     具名人工批准 → 一个事务写入 quote_header/quote_line → quote-r{n}.docx
```

任务状态（`process_status`）与商业结论（`commercial_disposition`）分开记录：前者是执行进度，后者是 `QUOTE` / `PARTIAL_QUOTE` / `NO_BID` / `SUPPLIER_INQUIRY`。同一件事「跑到哪一步」和「这单能不能报」不会互相覆盖。

## 设计约束

1. **数字只来自确定性引擎。** 定价在 `domain/pricing.py`，是一组纯函数；模型不参与任何计算。未接数据源时 `PendingQuoteProvider` 返回 `null`，不做估值。
2. **每个结论都能回到原文。** 行项目带 `evidence`，规则检查带 `locator`，定价决策带 `basis` 与候选来源。页面上点得进原邮件，报告里写得出「第 23 行 G 列」，JSON 里存得下。
3. **不确定就停下，而不是猜。** 缺料号、缺价格依据、要求证书但库存无证书记录、单位不一致、非精确件号匹配——都进复核项或标注为未报价，不会用默认值把行数凑齐。
4. **批准必须具名。** `activate_quote` 拒绝 `system:*` 与自动默认答案，只接受确认过的自然人；默认策略下批准被直接关闭，需显式打开演示模式。

## 解析与理解

### 四种输入载体

| 载体 | 实现 | 说明 |
|---|---|---|
| 正文 HTML 表格 | `html.parser` | 取第一个 `<table>`，表头按别名归一 |
| 正文制表符 | 按表头行切列 | `text/plain` 的真制表符分隔 |
| `.xlsx` 附件 | openpyxl | 可走 `openpyxl` 依赖或 `.deps/` 本地缓存 |
| `.pdf` 附件 | `ingestion/pdf_text.py` | 纯标准库：按 `Tm` 定位的 y 分行、x 排序列重建表格；不支持的结构直接报错，不假装能解 |

四种载体归一到同一套列名，因此只有一个 `row → Line` 映射。单位按原文保留与比较（PDF 的 `Each`/`PCS`/`SET` 不被改写成 `EA`），需要换算时进复核而不是静默换算。

### 模型读、代码审

`llm/perception.py` 把「读懂这封邮件」交给模型，把「值是不是真的」留给代码：

- 模型返回每个字段的 `value` / `raw` / `anchor` / `confidence`；
- 代码要求料号与数量能在 `anchor` 指向的那一行里找到，数量必须解析为正数；
- 置信度 ≥ 0.95 直接采用，0.70–0.95 采用但标记待复核，< 0.70 该行拒收——**不猜**；
- 认不出的列与读不懂的行分别落到 `unknown_columns` / `unparsed_rows`，不静默丢弃。

模型未配置、不可达或拒答时，自动回退到确定性解析，实际走的是哪条路径写进任务 trace，所以「是模型读的」这件事可核对而非声称。整条链路的算术仍然只发生在定价引擎里。

## 合规与业务规则

客户的「询价产品要求」逐行进定价，不是文案：

- `CERT REQUIRED` 类要求只接受 EASA Form 1 / FAA 8130-3；PMA、CoC 判 `cert_mismatch`；
- 库存表没有证书记录，客户要求证书时判 `cert_unverified`，不能自证满足；
- `SPECIAL REQUIREMENT` 内容未说明时单独生成待确认项；
- 按最低价选中 PMA 件时也要人决定（接受 PMA / 换有适航证书的货源 / 要求补 8130-3 / 不报此行）；
- 件号先精确匹配，再按大小写、空格、横杠归一或 MPN、手册件号匹配；非精确匹配必须人工确认。

两层核查分工明确：

| 层 | 模块 | 检查什么 |
|---|---|---|
| 契约自检 | `domain/evaluation.py` | 每行有结果、来源可追、金额 = `policy.line_amount()` 四舍五入到分 |
| 业务规则 | `domain/business_rules.py` | 件号匹配 / 证书 / 单位 / 件状 / 报价截止，逐行逐规则记 pass / warn / fail / not_applicable |

业务规则保留**通过项**，所以分析报告能列出「核查了什么」，而不只是列问题。只有已报价行违反硬规则（fail，且无人工决定记录）才并入 `evaluation_issues`——这类属于引擎缺陷。

## 人工复核门

自动环节一路跑到「需要人决定的地方」为止，阻塞项转成可回答的条目：

- 复核项 ID 由目标推导（`RI-0016` = 第 16 行，`RI-TASK` = 全案），答完不重新编号，否则决策日志会指向错对象；
- 阻塞项以 `interruptions` 持久化，回答后从原处续跑，不是从零重算；
- 修订号（revision）乐观锁：拿旧 revision 的动作会被拒绝，而不是覆盖别人的决定；
- 自主档位只有两个：`interactive`（停在待确认）与 `auto_draft`（按预设默认值解算：转人工、不超额采购、不放宽件状——不会为凑数放宽约束）。

## 对话层（可选）

`llm/genesis.py` 是一个 OpenAI 兼容的客户端加工具循环，纯标准库，可作为页面上的自然语言入口：

- 工具只返回引擎已有结果，或记录人已经作出的决定：`list_cases`、`start_case`、`get_task_overview`、`list_task_issues`、`get_review_item`、`get_lines`、`explain_line`、`get_history`、`resolve_review`、`export_draft`、`activate_quote`、`record_feedback`；
- 返回给模型的载荷按页压缩（122 行不会整表进上下文），「整单有什么问题」走统计概览而不是遍历详情；
- 模型不能算价，也不能自己批准：`activate_quote` 的 `approved_by` 必须是明确的人；
- 每轮有工具调用次数与轮数上限，超限即停，不无限自旋。

未配置模型时，页面按钮流程、确定性解析与导出全部照常可用，对话框提示缺哪两个值——降级不是白屏。

## 报告与导出

产物在 `runtime/<task_id>/`：

| 文件 | 产生条件 | 内容 |
|---|---|---|
| `draft-r{n}.json` | 每次定价 | 证据、决策日志、分析结构 |
| `analysis-r{n}.docx` | 每单必出 | 询价分析报告；标题从不是「报价单」 |
| `quote-draft-r{n}.docx` | 具备报价条件时 | 官方模板报价草案，首页与备注均标注「待复核草案」 |
| `quote-r{n}.docx` | 人工批准后 | 正式报价，同时写入 `quote_header` / `quote_line` |

分析报告包含摘要计数、逐行状态与原因、建议动作、原文定位，以及一张「规则核查覆盖」表（每条规则的通过 / 需关注 / 违规 / 不适用数量）。客户要合规声明时，附待签署段落。

批准走单个 SQLite 事务，渲染失败整体回滚；默认策略下批准关闭，演示时显式设置 `RFQ_DEMO_APPROVAL=1`，其产物统一标注「演示批准」，不会与正式商务批准混淆。

## 数据层

两张参考表（价格资源表 342 行、内部物料成本表 163 行）只在**源文件内容变化时**重新读取，之后所有请求走 SQLite（`runtime/rfq.db`）：

```text
snapshot   数据快照，由两个 xlsx 的哈希决定，只增不改
price_row  342 行，按料号建索引
cost_row   163 行，按料号建索引
mail       邮件解析结果，按文件 mtime 判断是否重解析
inbox      收件箱状态：收了哪几封、走到哪一步、对应 task_id
quote_header / quote_line   已批准的报价历史
```

实测首次 ETL 3.06 s，此后 0.009 s；列收件箱从 3.12 s 降到 0.066 s。报价时只取本单涉及的料号（A 命中 224/342 与 108/163，C 命中 0）。启动时后台预热 openpyxl、ETL 与收件箱，把一次性成本从「第一次点击」挪走。

执行过程另有独立账本 `runtime/operations.db`：追加写、带 `BEFORE UPDATE` / `BEFORE DELETE` 触发器，记录任务快照、来源证据、规则判定、人工决定、产物与结果反馈。账本只记录发生了什么，**不会把反馈自动提升为定价规则**。

## 配置

| 变量 | 默认 | 作用 |
|---|---|---|
| `PORT` | `8765` | 监听端口（PaaS 注入） |
| `RFQ_HOST` | `127.0.0.1` | 绑定地址；容器/PaaS 设 `0.0.0.0` |
| `RFQ_DEMO_APPROVAL` | 空 | 设为 `1` 打开演示批准（产物标注「演示批准」） |
| `GENESIS_BASE_URL` | `https://token.moi.matrixorigin.cn/v1` | OpenAI 兼容接口地址 |
| `GENESIS_MODEL` | 空 | 模型名 |
| `GENESIS_ACCESS_TOKEN` | 空 | 访问令牌（不回显、不入库） |

模型配置放 `.env`（已 gitignore）或同名环境变量，环境变量优先。未配置 token 时，页面按钮流程与确定性解析照常可用，对话框会说明缺哪两个值——降级不是白屏。

定价策略集中在 `domain/policy.py`，含 `policy_version`、加价率、汇率与 `pma_requires_review` 等键；每个报价都记录产生它的策略版本与数据快照 ID。**表中的参数是示例假设，不是经过批准的价格手册**，这也是默认关闭批准的原因。

## HTTP 接口

只读接口（`GET`）：

| 路径 | 用途 |
|---|---|
| `/healthz` | 健康检查：素材目录、`runtime/` 可写 |
| `/api/cases`、`/api/material`、`/api/fixtures` | 收件箱、可导入素材、官方案例 |
| `/api/task`、`/api/lines`、`/api/reviews`、`/api/report` | 任务视图，行项目/复核项/报告均分页 |
| `/api/extraction`、`/api/source` | 解析审计与原件定位 |
| `/api/analysis`、`/api/draft-preview`、`/api/history` | 分析报告、草案预览、执行历史 |
| `/api/agent`、`/api/data` | 模型配置状态、数据快照状态 |

写接口（`POST`）：`/api/preview`、`/api/upload`、`/api/import`、`/api/run`、`/api/resolve`、`/api/export`、`/api/activate`、`/api/feedback`、`/api/chat`、`/api/reset`。

`/api/activate` 要求 `approved_by` 为具名的人，且任务已无阻塞项、评估完成；不满足时失败关闭并说明原因。

## 测试

```powershell
python -m unittest discover -s tests -v
```

180 个用例覆盖：四种载体解析与 golden 对比、定价选源与失败关闭、合规门（证书/PMA/单位/件号）、复核与中断续跑、导出与批准事务、账本只追加、分页接口与对话上下文预算。`tests/fixtures/expected/` 下存有客户 B 的完整期望结果，任何解析或定价口径变化都会让它红。

## 示例数据

`data/询价信息素材/` 含六封官方询价邮件（脱敏演示数据），覆盖四种载体与两种业务日期：

| 案例 | 载体 | 行数 | 有价格依据 | 主要解析发现 |
|---|---|---|---|---|
| A | 正文 HTML 表格 | 122 | 109 | 10 个料号重复（14 行）、EA/IN 混用、34 行缺优先级 |
| B | 附件 `.xlsx` | 21 | 19 | PN-0001 重复、EA/IN 混用、6 行缺优先级 |
| C | 附件 `.pdf` | 7 | 0 | 3 条邮件条款 + 4 条 PDF 响应要求 |
| D | 正文 HTML 表格 | 122 | 0 | EA/FT 混用、30 行缺优先级 |
| E | 附件 `.xlsx` | 21 | 0 | EA/FT 混用、5 行缺优先级 |
| F | 附件 `.pdf` | 7 | 0 | 3 条邮件条款 + 4 条 PDF 响应要求 |

案例不是写死的列表：`cases.py` 扫描 `data/询价信息素材/**/*.eml`，案例字母取自买家地址（`rfq-a@…`），新增一封邮件不需要改代码。首次启动收件箱里只有客户 B 一封，其余在「可导入素材」里，点一下才进来；也可以拖入自己的 `.eml`，走同一条链路。

**C/D/E/F 为 0 行有价**：两张参考表覆盖 `PN-*` 料号（342 条价格里 276 条是 `PN-`）与标准件（`NAS`/`MS`/`AN`…），而 D/E 是 `AP-*`、C/F 是 `GSK-*`/`CPL-*`，表里确实没有这些料号。这不是解析失败——7 行、122 行都完整解出来了——而是没有价格依据。这类案子只产生**一个**任务级问题（整体转人工询价），不会把同一句问题重复 122 次。

## 目录结构

```text
rfq_agent/
  domain/          数据模型、定价、合规、业务规则、质量检查、策略
  application/     RFQService 流程编排、案例发现与收件箱
  ingestion/       邮件、表格、PDF 解析与数据归一
  infrastructure/  SQLite、任务存储、审计账本、可选依赖加载
  llm/             模型客户端、输入理解、工具循环与任务上下文
  reporting/       询价分析报告与报价 DOCX 导出
  static/          Web 页面资源
  web.py           HTTP 服务入口
  cli.py           命令行入口
  checkenv.py      环境检查入口
tests/             回归测试与 golden 期望
tools/             素材核对与文档生成脚本
refer_doc/         架构、评审与交付文档
examples/          示例输入
data/              官方询价素材（随仓库发布）
runtime/           本地数据库、任务与导出产物（不纳入 Git）
```

内部模块使用明确的包路径（`from rfq_agent.application.service import RFQService`），不提供旧的平铺导入兼容别名；新增代码按职责放入对应目录。

## 已知限制

1. **单进程顺序写。** 存储不支持多用户并发写；对话轮次已移出存储锁，避免长任务卡住其他请求，但真正的高并发需要换掉 `TaskStore` 的锁模型。
2. **条款已核对，未回灌定价。** 「报价有效期 30 天」这类条款会出现在分析报告与核对表里，但尚未改变报价有效期的计算。
3. **出口管制等合规判定只提示。** 系统不会自动判定是否受管制，只把它作为任务级确认项抛出。
4. **PDF 解析面向官方样本结构。** 依赖显式 `Tm` 定位与无压缩流；遇到其它生成器的 PDF 会明确报错，而不是降级猜测。
5. **只有 A、B 能跑通报价闭环。** 其余四例的料号不在参考表内，验证的是「敢说没有价格依据」，不是完整报价链路。

## 相关文档

- [架构图集](refer_doc/RFQ_ARCHITECTURE_DIAGRAMS.md)（六张图，另有离线单页 `refer_doc/architecture-diagrams.html`）
- [方案设计](refer_doc/RFQ_SOLUTION_DESIGN.md) · [行业无关基准](refer_doc/RFQ_BASELINE_DESIGN.md)
- [业界对标](refer_doc/RFQ_INDUSTRY_BENCHMARK.md) · [架构评估](refer_doc/RFQ_ARCHITECTURE_ASSESSMENT_2026-11-03.md)
- [需求清单](refer_doc/RFQ_REQUIREMENTS_BACKLOG.md) · [开发计划](refer_doc/RFQ_DEVELOPMENT_PLAN.md)
- [部署说明](refer_doc/RFQ_DEPLOYMENT.md) · [文档索引](refer_doc/README.md)