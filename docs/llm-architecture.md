# LLM 架构

本文只描述架构，不改运行代码。对照 `main` @ `26cc819`。  
「现状」是仓库里已经发生的事。「目标」是建议的下一阶段，尚未实现。

产品是询价代理，不是可选的解释插件。模型负责读懂和起草；规则负责算数和门禁。没配密钥、超时或返回坏 JSON 时必须能跑完，但界面和分析报告要标明这是降级，不能假装模型已经读过邮件。

## 1. 模块与职责

| 模块 | 现状职责 | 不负责 |
|---|---|---|
| `ingestion/ingest.py` | 读 `.eml`，按封闭别名表把表头收成规范列，抽出编号条款 | 定价、调用模型 |
| `ingestion/pdf_text.py` | 只解官方 PDF 用的那种子集（显式 `Tm`、无压缩或 Flate） | 通用 PDF |
| `ingestion/sources.py` | 行字典 → `RFQ`；证书只从「询价产品要求」列的原文用词表识别 | 正文条款、模型输出 |
| `llm/genesis.py` | 唯一的 HTTP 客户端；`.env` 后于环境变量；`Session` 工具循环（最多 6 轮） | 算价 |
| `llm/perception.py` | 解析成功走 `audit`（1 次）；解析失败走 `extract`（每 50 行 1 次） | 写单价、金额、MOQ、交期 |
| `llm/agent.py` | CLI `agent` 的 Deep Agents / LangChain 适配，可选依赖 | Web、`.env`、perception |
| `domain/pricing.py` | 纯函数定价；缺记录就是无价，不估值 | 读模型句子 |
| `domain/policy.py` | 行金额只经 `line_amount()`：单价 × 计价数量，四舍五入到分 | — |
| `domain/business_rules.py` | 件号 / 证书 / 单位 / 件状 / 报价截止，每条带定位 | 阻塞门禁本身 |
| `domain/terms.py` | 对 `rfq.requirements` 做子串核对 | 改金额 |
| `application/service.py` | 校验、定价、人工复核、导出、批准入库 | 调用模型 |
| `reporting/analysis.py` | 每单一份分析报告；无价行动作是固定中文模板 | 调用模型 |
| `web.py` + `static/index.html` | 六步台。只有 `POST /api/run` 和 `/api/upload` 会进 perception | 预览、打开已有任务不进模型 |

六步名称保持不变：输入采集、结构提取、询价分析、人工预审、分析报告、报价草案。

Deep Agents 与 `Session` 是两套工具循环：工具不同、超时不同、前者不读 `.env`、也不跑 perception。目标只留 `Session`。`llm/agent.py` 冻结，CLI `agent` 不再把它当作产品路径。理由：评委和页面只应看到一条轨迹；两套循环无法同时保证「数字只来自工具」。

## 2. 数据流

### 2.1 现状

```text
页面「运行」
  web._understand
    缺 GENESIS_MODEL 或 TOKEN → ingest_eml（0 次模型），source=deterministic
    有密钥 → perception.run
      ingest 成功 → audit（正文 + 附件名/行数 + 前 8 行摘要，无单价）
      ingest 失败 → extract（分页原文）→ validate 后才 build_rfq
      模型异常 → 再 ingest_eml，source=deterministic
  service.start_task
    analyze_rfq          只重校验结构
    build_quote_options  pricing + business_rules + 规则产生的复核项
  record_extraction      写入 task.extraction，并记一条 input_understood
```

`/api/preview`、收件箱打开已有任务、案例 B 的参考任务不经过 perception。对话 `POST /api/chat` 另走 `Session`，不在「运行」里。CLI `demo` / `case` 不调用模型。

audit 会改 RFQ：去重后把 `requirements` 追加进 `baseline.requirements`（`source=模型审查`），行、数量不动。这些句子随后进入 `terms.check`。可疑行只进 `extraction.issues`，再画到结构提取的「模型识别疑点」，不进 `open_reviews()`，因此不挡住定价。

extract 是例外。校验通过的数量会进入 `build_rfq`，成为定价输入。单价仍由引擎算。官方样例解析都能成功，正常演示不走 extract。

定价不读 `extraction.judgement`，也不读模型写的数字。`activate_quote` 仍要具名的人，拒绝 `system:*`。分析报告的建议动作来自 `ACTIONS` 字典，不引用模型。

没配密钥时流水线是 0 次调用，按钮仍可用。轨迹文案在 `index.html` 里把 `input_understood` 固定写成「模型读邮件」，与 `extraction.source` 无关。这是现状的不诚实之处。`.env.example` 写「不自动加载 .env」，`load_config` 实际会读仓库根的 `.env`。README「对话层」仍列 7 个工具；代码里 `tool_specs` 是 10 个（多了 `activate_quote`、`get_history`、`record_feedback`），也没写 perception 已在主路径上。

### 2.2 目标

默认就是模型开。模型关只用于两件事：证明模型没有改数字的测试基线，以及密钥 / 配额 / 网络 / 坏 JSON 时的降级。降级必须写进 `extraction`（`model_ran=false`、原因），并出现在询价分析和分析报告上，不能只藏在 trace 里。

```text
运行（默认）
  1. 规则先读邮件。读成了：模型做审核（分类、条款、可疑行），格子里的数保留。
     读不成：模型只映射表头；映射通过后仍用规则读单元格。
     映射也失败：才整页 extract，且每个料号/数量都要在原文里找到。
  2. 模型把正文要求抽成带引用的结构；没落地的句子丢掉。
  3. 规则定价、出阻塞复核项。模型不参与。
  4. 对规则定不出价的行，模型出分析卡（类别、寻源、要问客户的问题），不写回价格。
  5. 件号像目录里的另一条时，模型只能建议；人点确认前不改计价件号。
  6. 需要回信时，模型起草，标成草稿，不代发，正文不得出现金额。
  任一步失败 → 停在该步已有的规则结果上，报告标明降级，整单不崩。
```

人工预审之后才允许 `activate_quote` 写入历史库。这一点不动。

## 3. 模型可以做什么，不可以做什么

可以做的都是理解或起草，并且要能引用：

- 文档是不是询价，以及依据原文。
- 表头对应哪一个已有规范列（不发明列，不返回单元格）。
- 哪些客户要求是真的（证书、包装、交付条款、特殊要求等），每条带引用和定位。
- 哪一行可疑、为什么需要人看，引用必须在该行或正文里。
- 无价行的类别、寻源建议、要问客户的问题。
- 件号 / 物料相对目录的语义建议（候选必须已在本次取出的件号集合里）。
- 给客户或供应商的草稿（草稿，不发送）。
- 对话里决定调哪个工具；数字只复述工具结果。

不可以做的：

- 单价、金额、数量、MOQ、交期天数、库存、汇率。数量只在 extract 的兜底路径里抄录，且必须在锚点行找到；抄录不是计算。
- 单位换算。`EACH` 与 `EA` 的既有同义词可以保留；不得把 `PCS` / `SET` / 「件」折成 `EA`。
- 把没在原文出现的要求写进 `terms.check`。
- 把证书建议写进 `Line.required_certificates`。那一列是定价输入，改了就会改资格和金额。
- 自动把「像这个件号」写成匹配结果。人未确认前，行保持规则结论（通常是无价）。
- 在批准前写报价库，或替用户选复核项。

规则只做两件事：用已落地的字段计算，以及决定商业门禁（无价、证书不符、单位不一致、MOQ、件状、人工批准）。模型标出的可疑行是给人看的，不改 `unit_price` / `line_total`，也不新增一条能挡住金额的阻塞门。

## 4. 哪些规则是通用的，哪些是按样本拟合的

通用，换一封同领域的询价也不该改：

- 行金额四舍五入到分（`policy.line_amount`），评估时用同一函数重算。
- 正数数量、缺价格记录则该行不进报价、缺汇率则失败关闭、不估库存。
- 件号先精确再归一（大小写 / 空格 / 连字符）或 MPN、手册件号；非精确必须人确认。
- 证书是否满足、单位按原文比较、件状集合、报价截止。这些假定字段已经抽出来了。
- 定位形状：邮件 → 附件 → 表/页 → 行 → 列 → 原值。
- extract 路径上「料号必须在原文出现、数量必须是正小数、置信度分段」。
- 人工批准才入库。

按已知样本拟合，不能再往里加新词：

| 位置 | 拟合在哪里 |
|---|---|
| `HEADER_ALIASES` | 封闭词表。单元格必须整格命中，没有「件号 Part No.」这种中英混合 |
| `_rows_from_text` | 表头行必须含字面量 `Part Number`，且用制表符 |
| `_rows_from_xlsx` | 只读第一个工作表。`xlsx_sheets` 会列出全部表，但确定性解析不用 |
| `pdf_text.py` + `PDF_ITEM` / `PDF_QTY` | 文件自己写明不是通用 PDF。行形态是 `0001 \| PN`，下一行描述，再下一行 `数量 Each\|EA\|PCS\|SET`；还认 `Report date:`、`Respond by:`、`FOR ` |
| `_requirements_from_body` | 只收 `1.` / `2.` 这种编号清单 |
| `_requirements_from_pdf` | 最后一个 `remarks` 之后、以句号结尾的句子，最多 8 条 |
| `terms.RULES` | 注释写明取自两封英文样例：`delivery lead time`、`validity period`、`compliance declaration` 等 |
| `cases.BUYER`、`SEED=("B",)` | 案例字母来自 `rfq-a@`；收件箱默认只放 B |
| `discover` | `ingest_eml` 抛错的邮件直接跳过，不会出现在收件箱 |

边界：`required_certificates` 的词（`8130`、`EASA`、`CERT`、适航、证书）是领域词表，不是某一封信的版式。它只应跑在已经抽出来的原文上，不能靠加关键词去迁就新邮件。`RFQ.currency` 默认 `CNY` 是报价币种的引擎默认值；和提示词里写死 `"currency": "CNY"` 不是一回事。后者是模型在补原文没有的字段，目标里删掉。

HTML 路径比 README 写的更宽：会逐个表格试别名，不是死守第一张表。别名仍是封闭的，所以换列表头一样失败。

不要为了让未见过的询价解析成功去加别名、PDF 行正则或条款短语。那些差异交给模型，代码只做校验。

## 5. 模型调用的契约

现状没有独立模板，也没有 schema 校验。JSON 是从第一个 `{` 截到最后一个 `}`。无重试、无缓存、无 `response_format`。`temperature` 只在 `Genesis` 上为 0。

目标：所有 HTTP 都进 `rfq_agent/llm/` 的一个入口。入口负责超时、429/5xx/超时/坏 JSON 重试一次、按输入哈希缓存（不含 token）、写调用轨迹、按 schema 收 JSON。调用方捕获失败并降级，不把异常抛成白屏。引用类字段统一做规范化子串检查（NFKC、合并空白、casefold）；对不上就丢弃并记入 `issues`。

缓存键 = 模型名 + 用途 + temperature + messages + tools + schema。测试用录好的响应填缓存或替换传输层，保证开/关模型时金额逐字节相同。

### 5.1 `audit`（现状，解析成功时的那一次）

- 输入：正文类块（邮件文字、HTML 表、PDF/DOCX/TXT，单块最多 400 行）、xlsx 只给文件名和行数、解析摘要前 8 行、已识别列名。不含价格本、成本、汇率、算出的单价。
- 输出：`document_type`、`document_confidence`、`requirements[]`、`unknown_columns[]`、`suspicious_rows[{row,why}]`、`unparsed_rows[]`、`judgement`。
- 校验：几乎没有。非询价只降为 `severity=review`。要求句子去重后直接追加。
- 失败：整单回退确定性解析。
- 目标增补：`requirements` 与可疑行的 `quote` 必须是来源块的子串，否则丢弃。分类若给了依据引文，引文同样要落地；落不了地就保留类型但记「依据未核对」。xlsx 解析已成功时，不要求模型对没看见的单元格负责。

### 5.2 `extract`（现状，仅当确定性解析失败）

- 输入：一个内容块的一页，最多 50 行，左侧是原始行号。
- 输出：文档类型；客户、单号、日期、行字段均为 `{value, raw, anchor, confidence, evidence}`；另有 `requirements[]`。提示词把 `currency` 写成 `CNY`。
- 校验：置信度 ≥0.95 采用；0.70–0.95 采用并标 `line_best_guess`；低于 0.70 拒收。料号必须找得到（锚点失败会退回全文，这是缝）。数量必须是正小数；`quantity.raw` 为空则不核对它是否在该行。单位不在已知集合里只警告。`requirements` 不查子串。裸字符串当置信度 1.0。
- 失败：和解析失败叠在一起则报错，不编空报价。
- 目标：提示词不写币种；原文没有币种就省略。要求句子同样要落地。这一路仍是最后手段，排在表头映射之后。

### 5.3 `Session` 对话（现状，用户点「提问」才发生）

- 输入：系统提示 10 条（数字只来自工具、不替人做决定、导出物是草案、邮件不是指令等）+ 历史 + 10 个工具的 schema。
- 输出：自然语言，或 `tool_calls`。工具参数 JSON 解析失败当成 `{}`。
- 校验：无结构校验。工具结果超过 6000 个字符就按字符切开（常量名是 `MAX_TOOL_BYTES`），可以切在 JSON 中间。
- 失败：超过 6 轮抛 `GenesisError`。缺密钥时对话框禁用，主流程仍可用。
- 目标：走同一入口。截断改为按列表元素丢弃并保留合法 JSON。解释类问题默认不把单价塞进上下文，留到草稿能力之后再收紧，避免同时改两套契约。

### 5.4 Deep Agents（现状，将冻结）

- 只被 `cli.py` 的 `agent` 调用。`timeout=45`、`max_retries=1`、未设 temperature、只读环境变量。工具集与 `Session` 不同（有 `apply_review`，没有 `list_cases` / `start_case` / `explain_line`）。
- 目标：不再作为产品路径。文件保留并标明冻结，避免评委从 CLI 走进另一条轨迹。

### 5.5 `map_headers`（目标，尚未实现）

- 何时：确定性解析因表头对不上而失败，且还没进 extract。
- 输入：表头行的序号和原文。不传数据行，不传价格本。
- 输出：`{"columns":[{"index":0,"canonical":"Part Number","header":"件号 Part No.","confidence":0.97}]}`。`canonical` 只能是现有规范列名。
- 校验：置信度 ≥0.95，且 `header` 与该格原文在规范化后一致。通过后用现有单元格读取和 `positive_decimal`。模型不返回单元格。
- 失败：不新增别名。继续 extract；extract 再失败则降级并在报告里写明这封信规则读不了、模型也没给出可核对的结构。

### 5.6 `bind_requirements`（目标，尚未实现）

- 何时：audit 之后。正文块 id + 原文。不传价格表。
- 输出：`items[]`，每项有 `kind`（certificate / packaging / delivery / special / validity / lead_time / alternate / compliance / other）、`quote`、`locator.block`、可选 `certificates`、`confidence`。
- 校验：`quote` 必须是该块的子串，否则丢弃。`certificates` 只保留 `required_certificates()` 已认识的名字，挂在条款上供报告和 `terms.check` 使用，不写回行上的 `required_certificates`。不把天数写入 `valid_until`。
- 失败：只留编号正则已经抽出的条款。
- 与现状的差别：今天 audit 的自由句子会直接进入条款核对，幻觉的 “validity period …” 会变成一行「需人工确认」。目标是没引用的句子进不了核对。

### 5.7 无价行分析卡（目标，尚未实现）

- 何时：定价之后，原因里有 `no_price_record` 的行，一次批量调用。
- 输入：件号、描述、单位、件状、客户要求原文、规则核查摘要。不传成本表和单价。
- 输出：`cards[]`：`line_id`、`category`（consumable / rotable / unknown）、`sourcing[]`、`questions[]`、`evidence_quote`。schema 禁止 `price`、`unit_price`、`amount`、`line_total`、`moq`、`lead_time`、`lead_time_days`、`quantity`、`cost`，且 `additionalProperties: false`。
- 校验：`evidence_quote` 必须在该行描述或要求里。卡片只进询价分析和分析报告，不进 `eligibility`。
- 失败：保留现有 `ACTIONS` 模板，并标明模型卡未生成。

### 5.8 件号建议（目标，尚未实现）

- 何时：无精确命中的行。只把本次查询已经取出的件号字符串列表交给模型，不含价格列。
- 输出：`suggestions[]`：`line_id`、`candidate_pn`、`reason`、`confidence`、`quote`（询价里的原件号）。
- 校验：`candidate_pn` 必须在该集合里，否则丢弃。不写入 `resolutions`。界面只给现有的 `confirm_match` 加一句人话。人未点则行保持无价，金额与模型关一致。
- 失败：不显示建议。

### 5.9 回复草稿（目标，尚未实现）

- 何时：分析步骤显式生成，不进 `start_task`，不代发。
- 输入：客户、主题、条款核对里待补齐/需人工的原文引用、无价行的件号和数量（数量来自 RFQ）。固定声明「草稿，待复核，不是报价」。
- 输出：`kind`（buyer_reply / supplier_rfq）、`subject`、`body`、`citations[]`。schema 拒绝价格字段。`citations` 必须是输入里已有的落地引用。
- 校验：正文若匹配货币金额则整封丢弃。
- 失败：不展示信，只留分析报告。

## 6. 已定、但还没写进代码的决定

1. 一个入口。audit、extract、对话和后面的新调用都走它。不再维持第二家客户端。
2. 冻结 Deep Agents，保留 `Session`。见第 1 节。
3. 引用不落地就丢弃，并记 issue。这包括 audit 的要求句子和可疑行，不只是 extract 的料号。
4. 轨迹只在 `model_ran` 为真时写「模型读了邮件」。降级要出现在询价分析和分析报告上，用白话写原因（未配置、超时、返回不是 JSON、引用被拒）。
5. 工具结果按 JSON 结构缩短，不按字符中间切开。
6. 提示词里删除写死的 `CNY`。引擎报价币种默认值不动。
7. 模型建议的证书和件号都不进定价输入。条款核对和分析报告可以展示；金额必须与模型关逐字节相同。这是对审查建议 5.1 的收窄：审查写「证书再交给 `required_certificates`」，若写进行字段会改变资格和金额，与黄金测试冲突。
8. 泛化测试用同一套目录列（件号、单位、成本、价格状态），造未见过的变体：表头改名或换序、中英混合、要求只在正文、多列或合并列、单位写法不同、件号错一字、多表、非官方版式的 PDF。规则失败是预期。模型开使用录制响应。两边都能计价时，金额必须相同；只有表头被映射成功时，金额应等于「同一批格子、规范表头」的规则结果。不要为这些变体加别名或正则。官方六封信的素材不在 git 里，那组黄金测试在素材缺失时跳过；合成变体放在测试夹具里，保证没有私有数据也能跑。有真实密钥时另跑一遍活的，结果写进覆盖表，不作为断言。

和审查不一致的地方就这些：证书不写回行、件号不自动匹配、Deep Agents 不接入 Web、xlsx 审核没看见格子就不能声称读懂了表、提示词币种和引擎默认币种分开看。审查对「audit 没有落地校验、JSON 截取很脆、对话会切坏 JSON、两条栈在漂移、轨迹文案不诚实」的判断与代码一致。

## 7. 实施顺序

还没做。每一段单独提交，上一段的测试先过。

| 顺序 | 内容 | 不做的事 |
|---|---|---|
| 0 | 本文 | 不改行为 |
| 1 | 单一入口：schema、重试、超时、缓存、轨迹；现有 audit / extract / 对话接上去；引用落地；冻结 Deep Agents；轨迹文案；结构化截断；提示词去掉币种；降级标记进 UI 和报告 | 不加新能力，不加别名 |
| 2 | `bind_requirements`：落地条款进 `terms.check` 和分析报告，不进行字段 | 不改金额 |
| 3 | 无价行分析卡，进询价分析和报告；schema 无价格字段 | 不进 `eligibility` |
| 4 | 表头映射、目录内件号建议（人确认前不生效）、合成泛化集和覆盖表 | 不为样本加硬编码 |
| 5 | 回复草稿，引用、无金额、不代发 | 不进 `start_task` |

第 1 段之后就可以用录制响应证明：官方样例（素材在时）和规则能解析的合成单，模型开与模型关的单价、金额、报价数字逐字节相同，并与当前 `main` 相同。第 4 段的覆盖表写回本文，规则读失败的变体单独计数，不强行和一条不存在的金额比较。
