# MatrixNEXT RFQ Agent

输入是**素材本身**：六封官方询价邮件，正文表格或附件（xlsx / pdf）都走同一条链路。
输出是官方模板 DOCX 草案 + 每个数字的来源。23 个测试通过。

## 跑起来

```powershell
python -m rfq_agent.web --port 8765     # 浏览器打开 http://127.0.0.1:8765/
```

页面里选任一案例，或把一封 `.eml` 拖进去，走的是同一个解析 + 定价 + 复核 + 导出流程。

命令行等价入口：

```powershell
python -m rfq_agent.cli case --case B                              # A..F 六选一
python -m rfq_agent.cli case --case B --resolve RI-0016=quote_with_moq --export
python -m rfq_agent.cli case --case B --autonomy auto_draft --export
python -m unittest discover -s tests -v
```

产物在 `runtime/<task_id>/`：`draft-r<rev>.json`（证据 + 决策日志）和 `quote-draft-r<rev>.docx`。

## 六个案例怎么进来

| 案例 | 邮件 | 表格位置 | 行数 | 有价格依据 |
|---|---|---|---|---|
| A | `1/航空配件询价（RFQ-20260901）.eml` | 正文 HTML 表格 | 122 | 109 |
| B | `1/航空询价（RFQ-20260902）.eml` | 附件 `.xlsx` | 21 | 19 |
| C | `1/Request for Quotation - Spare Parts…eml` | 附件 `.pdf` | 7 | 0 |
| D | `2/航空配件询价（RFQ-20260911）.eml` | 正文 HTML 表格 | 122 | 0 |
| E | `2/航空询价（RFQ-20260912）.eml` | 附件 `.xlsx` | 21 | 0 |
| F | `2/Request for Quotation - Components…eml` | 附件 `.pdf` | 7 | 0 |

300 行全部解析成功。案例不是写死的列表：`cases.py` 扫 `data/询价信息素材/**/*.eml`，案例字母取自买家地址（`rfq-a@…`），新增一封邮件不需要改代码。

**C/D/E/F 为什么 0 行有价**：两张参考表覆盖的是 `PN-*` 料号（342 条价格里 276 条是 `PN-`）和标准件（`NAS`/`MS`/`AN`…），而 D/E 是 `AP-*`、C/F 是 `GSK-*`/`CPL-*`，表里没有这些料号。这不是解析失败——7 行、122 行都完整解出来了，是**确实没有价格依据**。智能体的正确行为是说出来，而不是编一个价：这类案子只产生**一个**任务级问题（"整体转人工询价"），不会给评委弹 122 个一样的对话框。

## 四种载体的解析

- **正文 HTML 表格**：`html.parser` 取第一个 `<table>`，表头按别名归一。
- **正文制表符**：D 的 `text/plain` 是真制表符分隔，按表头行切列。
- **`.xlsx` 附件**：openpyxl（走 `.deps` 可选缓存或正常 pip 安装）。
- **`.pdf` 附件**：`pdf_text.py`，纯标准库。官方 PDF 是 Helvetica + 显式 `Tm` 定位、无压缩流，按 y 分行、x 排序列即可重建成表格。**不依赖 pypdf**，也不会假装能解任意 PDF——遇到不支持的构造会抛错而不是猜。

所有载体归一到 `.xlsx` 附件那套列名，所以只有一份 `row → Line` 映射。每行都带 `evidence`：`{file, container, row}`，页面和 JSON 里都能追到"这行来自哪个载体的第几行"。

## 三条不可让步的约束

1. **数字只来自确定性引擎**（`pricing.py`，纯函数）。没有模型参与计算，未接数据时 `PendingQuoteProvider` 返回 `null` 而不是估值。
2. **人工确认边界**。有阻塞项时停在 `awaiting_review`；`auto_draft` 只按预设默认值解算（转人工、不超额采购、不放宽件状），不会为凑数放宽约束。
3. **导出物永远是草案**。首页与备注都写明"待复核草案"，并声明税费运费未计入、不预留库存。

复核项 ID 由目标推导（`RI-0016` = 第 16 行，`RI-TASK` = 全案），答完不重新编号，否则决策日志会指向错的对象。

## 分层

```text
ingest.py      .eml → RFQ（四种载体，一个出口）
pdf_text.py    标准库 PDF 文字提取（带坐标）
sources.py     xlsx 读表、row → Line、价格/成本表加载
pricing.py     确定性定价引擎：候选、硬约束、加成、复核项
service.py     业务动作 + 复核门 + revision + 产物
export_docx.py 官方模板填充 + 条件摘要页 + 金额大写
cases.py       案例发现（扫素材，不写死）
web.py         本地演示服务（标准库 http.server）
cli.py         demo / case / agent 三个命令
agent.py       Deep Agents + Genesis 适配（懒加载，可选）
```

## 还没做

1. **真正的对话层**：页面里的"过程"是按真实工具结果生成的叙述，不是 LLM。接 Genesis 后换成模型驱动，工具集已经就位（`analyze_rfq`/`build_quote_options`/`list_review_items`/`resolve_review`/`export_quote_draft`…）。
2. **MOI 托管入口**：挑战赛允许自建 Web，但若评审偏好 MOI 原生 Agent，需要另做接入。
3. 出口管制/合规的任务级确认项（只提示、不自动判定）。
4. 多用户并发（当前存储只支持单进程顺序调用）。

## 依赖

`.deps/` 是本地可选缓存（已 gitignore：python-docx / lxml / openpyxl）。存在时自动接到 `sys.path[0]`；不存在时 `pip install -e ".[documents]"` 正常安装。PDF 解析不需要额外依赖。

方案与进度见 `refer_doc/`：[方案](../refer_doc/RFQ_SOLUTION_DESIGN.md)、[业界对标](../refer_doc/RFQ_INDUSTRY_BENCHMARK.md)、[需求清单](../refer_doc/RFQ_REQUIREMENTS_BACKLOG.md)、[开发计划](../refer_doc/RFQ_DEVELOPMENT_PLAN.md)、[文档索引](../refer_doc/README.md)。

API 参考：https://docs.langchain.com/oss/python/deepagents/quickstart