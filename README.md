# MatrixNEXT RFQ Agent

输入是**素材本身**：六封官方询价邮件，正文表格或附件（xlsx / pdf）都走同一条链路。
输出是官方模板 DOCX 草案，每个数字都带来源；每单（无论能否报价）都附一份「询价分析报告」。121 个测试通过（1 个依赖 `.deps` 的用例默认跳过）。

## 跑起来

```powershell
python -m rfq_agent.web --port 8765     # 浏览器打开 http://127.0.0.1:8765/
```

公网部署（给评委的链接）：`docker build -t rfq-agent . && docker run -p 8000:8000 rfq-agent`，
或按 [README 部署文档](refer_doc/RFQ_DEPLOYMENT.md) 在 Render 用根目录 `render.yaml` 一键部署。
服务认 `PORT` / `RFQ_HOST` 环境变量，健康检查 `GET /healthz`。

首次启动收件箱里只有**客户 B** 一封（Excel 附件、21 行、19 行有价、2 项待确认）。
其余素材在左侧「可导入素材」里，点一下才进来；也可以把自己的 `.eml` 拖进去——
走的是同一个解析 + 定价 + 复核 + 导出流程。

每单的节奏是**半自动**：点「开始处理」后一路解析、匹配价格、过滤硬约束，
**停在需要人决定的地方**；待确认项答完才出现「生成报价草案」。

命令行等价入口：

```powershell
python -m rfq_agent.cli case --case B                              # A..F 六选一
python -m rfq_agent.cli case --case B --resolve RI-0016=quote_with_moq --export
python -m rfq_agent.cli case --case B --autonomy auto_draft --export
python -m unittest discover -s tests -v
```

产物在 `runtime/<task_id>/`：`draft-r<rev>.json`（证据 + 决策日志 + 分析结构）、
`analysis-r<rev>.docx`（询价分析报告，每单必出，标题从不是“报价单”）、可报价时另有
`quote-draft-r<rev>.docx`，人工批准后生成 `quote-r<rev>.docx`。

**合规门**：客户的「询价产品要求」逐行进入定价——`CERT REQUIRED` 类要求只接受
EASA Form 1 / FAA 8130-3（PMA、CoC 判 `cert_mismatch`）；库存表没有证书记录，不能自证满足；
`SPECIAL REQUIREMENT` 内容未说明，单独出待确认项；按最低价选中 PMA 件时也要人决定。
单位按原文比较（PDF 的 `Each/PCS/SET` 不再改写成 EA），不一致进复核、不换算；
件号先精确匹配，再按大小写/空格/横杠归一或 MPN/手册件号匹配，非精确匹配必须人工确认。
每条发现都带定位：邮件 → 附件 → 表/页 → 行 → 列 → 原值。

**两层核查**：`evaluation.validate(rfq, results) -> list[str]` 只做契约自检（每行有结果、
来源可追、金额 = `policy.line_amount()` 四舍五入到分）；`business_rules.check_business`
逐行逐规则（件号匹配 / 证书 / 单位 / 件状 / 报价截止）记录 pass / warn / fail / not_applicable，
存在 `Task.business_checks`，分析报告据此列出“核查了什么”，不只列问题。

**批准入库**：`activate_quote` 只接受具名的人（拒绝 `system:*` 与自动默认答案），
在一个 SQLite 事务里写 `quote_header/quote_line` 并渲染 `quote-r<rev>.docx`。默认策略
仍然拒绝激活；演示时显式打开 `RFQ_DEMO_APPROVAL=1`，产物统一标注「演示批准」。

## 六个案例

| 案例 | 邮件 | 表格位置 | 行数 | 有价格依据 | 解析发现 |
|---|---|---|---|---|---|
| A | `1/航空配件询价（RFQ-20260901）.eml` | 正文 HTML 表格 | 122 | 109 | 10 个料号重复（14 行）、EA/IN 混用、34 行缺优先级 |
| B | `1/航空询价（RFQ-20260902）.eml` | 附件 `.xlsx` | 21 | 19 | PN-0001 重复、EA/IN 混用、6 行缺优先级 |
| C | `1/Request for Quotation - Spare Parts…eml` | 附件 `.pdf` | 7 | 0 | 3 条邮件条款 + 4 条 PDF 响应要求 |
| D | `2/航空配件询价（RFQ-20260911）.eml` | 正文 HTML 表格 | 122 | 0 | EA/FT 混用、30 行缺优先级 |
| E | `2/航空询价（RFQ-20260912）.eml` | 附件 `.xlsx` | 21 | 0 | EA/FT 混用、5 行缺优先级 |
| F | `2/Request for Quotation - Components…eml` | 附件 `.pdf` | 7 | 0 | 3 条邮件条款 + 4 条 PDF 响应要求 |

300 行全部解析成功。案例不是写死的列表：`cases.py` 扫 `data/询价信息素材/**/*.eml`，
案例字母取自买家地址（`rfq-a@…`），新增一封邮件不需要改代码。

**C/D/E/F 为什么 0 行有价**：两张参考表覆盖的是 `PN-*` 料号（342 条价格里 276 条是 `PN-`）
和标准件（`NAS`/`MS`/`AN`…），而 D/E 是 `AP-*`、C/F 是 `GSK-*`/`CPL-*`，表里没有这些料号。
这不是解析失败——7 行、122 行都完整解出来了，是**确实没有价格依据**。智能体的正确行为是
说出来，而不是编一个价：这类案子只产生**一个**任务级问题（"整体转人工询价"），
不会给评委弹 122 个一样的对话框。

## 四种载体的解析

- **正文 HTML 表格**：`html.parser` 取第一个 `<table>`，表头按别名归一。
- **正文制表符**：D 的 `text/plain` 是真制表符分隔，按表头行切列。
- **`.xlsx` 附件**：openpyxl（走 `.deps` 可选缓存或正常 pip 安装）。
- **`.pdf` 附件**：`pdf_text.py`，纯标准库。官方 PDF 是 Helvetica + 显式 `Tm` 定位、
  无压缩流，按 y 分行、x 排序列即可重建成表格。**不依赖 pypdf**，也不会假装能解任意 PDF
  ——遇到不支持的构造会抛错而不是猜。

所有载体归一到 `.xlsx` 附件那套列名，所以只有一份 `row → Line` 映射。每行都带
`evidence`：`{file, container, row}`，页面和 JSON 里都能追到"这行来自哪个载体的第几行"。

## 解析质量与客户条款

`quality.py` 检查单据本身的不一致，全部来自数据、只看不改价格：

- 重复料号（只标记，不合并、不删除）；
- 单位混用（A 的 EA/IN、D 的 EA/FT）；
- 部分行缺优先级、交期早于评估日；
- 整列全空的按"该载体不含这一列"记录，不当缺陷（PDF 没有优先级列）。

`requirements` 抓客户在正文里用编号写的报价要求（C/F 各 3 条），
以及 PDF 表单上的响应指示（各 4 条），原文照录并标注出处。
`terms.py` 接着核对每条要求在草案里是否成立：已满足 / 待补齐 / 需人工确认 / 不适用，
例如"每行要有交期"会落到真实行数上（B 的 19 行里 5 行缺交期）。
两者都写进导出草案：要求核对成一张表，客户要合规声明时附待签署的合规声明段落。

## 数据层

两张参考表（价格资源表 342 行、内部物料成本表 163 行）只在**源文件内容变化时**读一次，
之后所有请求走 SQLite（`runtime/rfq.db`）：

```text
snapshot   数据快照（由两个 xlsx 的哈希决定，只增不改）
price_row  342 行，按料号建索引
cost_row   163 行，按料号建索引
mail       六封邮件的解析结果（按文件 mtime 判断是否重解析）
inbox      用户收了哪几封、走到哪一步、对应 task_id
```

实测：首次 ETL 3.06 s，之后 0.009 s；列收件箱从 3.12 s 降到 0.066 s；
报价时只取本单涉及的料号（A 命中 224/342 与 108/163，C 命中 0）。
启动时后台预热 openpyxl / ETL / 收件箱，一次性成本不落在"第一次点击"上。

## 三条不可让步的约束

1. **数字只来自确定性引擎**（`pricing.py`，纯函数）。没有模型参与计算，未接数据时
   `PendingQuoteProvider` 返回 `null` 而不是估值。
2. **人工确认边界**。有阻塞项时停在 `awaiting_review`；`auto_draft` 只按预设默认值解算
   （转人工、不超额采购、不放宽件状），不会为凑数放宽约束。
3. **导出物永远是草案**。首页与备注都写明"待复核草案"，并声明税费运费未计入、不预留库存。

复核项 ID 由目标推导（`RI-0016` = 第 16 行，`RI-TASK` = 全案），答完不重新编号，
否则决策日志会指向错的对象。

## 项目结构

```text
rfq_agent/
  domain/          数据模型、定价、合规、业务规则与质量检查
  application/     RFQService 流程编排、案例与收件箱操作
  ingestion/       邮件、表格、PDF 解析与数据归一
  infrastructure/  SQLite、任务存储、审计账本与可选依赖加载
  llm/             模型客户端、输入理解与 Agent 工具调用
  reporting/       分析报告与报价 DOCX 导出
  static/          Web 页面资源
  web.py           HTTP 服务入口
  cli.py           命令行入口
  checkenv.py      环境检查入口
tests/             回归测试
tools/             开发与素材核对脚本
refer_doc/         设计、评审与交付文档
examples/          示例输入与预期结果
data/              官方询价素材（随仓库发布，部署后才有案例可点）
runtime/           本地数据库、任务与导出产物（不纳入 Git）
```

启动命令保持不变：`python -m rfq_agent.web`、`python -m rfq_agent.cli`。
内部模块使用明确的包路径，例如 `from rfq_agent.application.service import RFQService`；
旧的平铺导入路径不再提供兼容别名，新增代码和测试按职责放入对应目录。
这是目录归类，保留现有业务调用关系；例如定价模块仍包含 SQLite provider，
模型工具仍复用 Web 结果视图，并未在本次整理中改写这些边界。

## 对话层（模型可选）

`rfq_agent/llm/genesis.py` 是 OpenAI 兼容的客户端 + 工具循环，纯标准库。
配置放 `.env`（已 gitignore）：

```text
GENESIS_BASE_URL=https://token.moi.matrixorigin.cn/v1
GENESIS_MODEL=deepseek-v4.1-flash
GENESIS_ACCESS_TOKEN=<你的 token>
```

模型只能调工具，不能算数：`list_cases` / `start_case` / `get_task` / `get_lines` /
`explain_line` / `resolve_review` / `export_draft`。每个工具返回的都是引擎的既有结果，
且返回给模型的载荷做了压缩（不塞 122 行原始数据进去）；`get_task` 里也带上了
客户条款与解析发现，所以问"这单有什么问题"时它能如实说，但不会据此改价。

实测（同一句"打开案例C…"）：

| 模型 | 耗时 | 工具调用 |
|---|---|---|
| `deepseek-v4.1-flash` | 8.7–12.5 s | 3 |
| `glm-5.3-flash` | 9.9 s | 2 |
| `qwen3.8-flash` | 18.1 s | 3 |
| `qwen3.8-max` | >180 s（超时） | — |

没配 token 时，页面上的按钮流程照常可用，对话框会提示缺哪两个值——降级不是白屏。

## 还没做

1. **MOI 托管入口**：挑战赛允许自建 Web，但若评审偏好 MOI 原生 Agent，需要另做接入。
2. 出口管制/合规的任务级确认项（只提示、不自动判定）。
3. 多用户并发（当前存储只支持单进程顺序调用；对话轮次已移出存储锁，避免长任务卡住其他请求）。
4. 条款已做"核对"，仍未回灌定价策略（如"有效期 30 天"未改变报价有效期计算）。

## 依赖

`.deps/` 是本地可选缓存（已 gitignore：python-docx / lxml / openpyxl）。存在时自动接到
`sys.path[0]`；不存在时 `pip install -e ".[documents]"` 正常安装。PDF 解析不需要额外依赖。

方案与进度见 `refer_doc/`：[方案](../refer_doc/RFQ_SOLUTION_DESIGN.md)、
[业界对标](../refer_doc/RFQ_INDUSTRY_BENCHMARK.md)、
[需求清单](../refer_doc/RFQ_REQUIREMENTS_BACKLOG.md)、
[开发计划](../refer_doc/RFQ_DEVELOPMENT_PLAN.md)、[文档索引](../refer_doc/README.md)。

API 参考：https://docs.langchain.com/oss/python/deepagents/quickstart