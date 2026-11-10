# 最新推送批次代码评审（2026-11-03）

> 评审对象：「最新一次推送」= 2026-11-02 21:00 推到 `HUIYULEO/rfq-agent` main 的一批工作，
> 对应本地提交 `c37e8e8 → 8c8674c`（c37e8e8 feat: streamline RFQ workbench、
> 3d41501 docs: audit RFQ business rules、6ede16f fix: gate RFQ drafts、8c8674c refactor: keep only RFQ evaluation structures），
> 净 diff `6e496d6..8c8674c`：18 个文件，+943/-199。
> 仓库里目前**没有任何 GitHub PR**（0 个 PR、0 fork、唯一分支 main），如"最新的 PR"另有所指请以补充的仓库/编号为准。
>
> 验证手段：95/95 单测全绿（`python -m unittest discover -s tests`）；B 案例 CLI 端到端复跑
> （21 行、19 行有价、内部 5 + 供应商 14、小计 386,595.80，与 golden 一致）；C 案例导出复跑；
> evaluation 边界探针；前后端契约逐条对照；文档对照代码逐条核实。
> 远端 main（f1befff）与本仓库 HEAD 在项目文件上逐文件一致（R100），本评审结论对已推送内容同样成立。

---

## 结论摘要

方向全对，落地九成。这批把「演示假设」从代码和文档里系统地剥离：fail-closed 的商业激活边界、
背对背的评估完整性自检、只产"分析记录"不冒充报价单的导出门禁、预览→确认两段式上传——
设计意图清晰且全部有测试锁定（test_v2_workflow 三连拒绝、test_export 反向断言、test_golden_b 导出门禁断言）。

需要修的是四个硬伤 + 一批一致性收尾：
1. **评估门禁只落地了一半**：后端发了 `evaluation_status`/`evaluation_issues`、新增了 `evaluation_incomplete` 状态，前端零消费，导出提示语义与真实产物相悖（🔴C1）。
2. **金额勾稽校验比引擎合同更严**：分数数量（2.5 EA）会把**正确的**引擎输出判成 `amount_consistency`，整单 evaluation_incomplete（🔴C2，探针已复现）。
3. **同一份 DOCX 内部矛盾**：条款核对表仍说"草案已留出「合规声明」段落，需签署后随报价发出"，而正文合规节已改为"本草案不作任何合规性声明"（🔴C3）。
4. **前后端列表链路无错误处理**：一处 5xx 即首屏/收件箱静默全灭（🔴C4）。
5. **证据文档 4 处"现存缺口"已被同批次自己修掉**：文档写于 3d41501、代码修于 6ede16f/8c8674c，未随扫尾（🔴D1，参赛对外资产，提交评审前必须刷新）。

---

## 🔴 必修（代码）

### C1. 评估/导出门禁的前后端契约只落地一半 —— 演示中可被评委抓到

- `web.py:138-139` `_view` 专门新增把 `evaluation_status`/`evaluation_issues` 发给浏览器，但 `static/index.html` 全文 **0 处引用**（grep 验证）。
- `static/index.html:537-539` 状态 pill 只映射 `awaiting_review`/`exported`/`draft_ready`，新状态 `evaluation_incomplete` 原样输出英文且无对应 CSS 类（`.pill.awaiting_review` 等定义在 78-79 行，无 evaluation_incomplete）。
- `static/index.html:549` 导出按钮可见性只看 `open.length === 0 || artifacts.length`；
  `static/index.html:681` 导出 toast 恒为「报价草案已生成，可在当前任务下载。」；
  `web.py:450-453` `/api/export` **无条件** `mark_stage("draft")`，收件箱打"已出草案"绿标。
- 但 `service.py:311-327` 的 `quote_draft_allowed`：disposition 为 SUPPLIER_INQUIRY / NO_BID、或评估不完整时**只写 draft-r{n}.json 分析记录、不产 DOCX**（注释明言 "must not be packaged as a quotation document"）。后端这套门禁是本批最大的语义进步，前端却把它说反了。

**复现（合法演示路径）**：所有行 resolve 为"转供应商询价"→ 无未决项、导出按钮可见可点 → 后端只产 JSON、状态置 "exported" → 前端弹"报价草案已生成"、收件箱标"已出草案"。
**建议**：render() 消费 `s.evaluation_status`/`s.evaluation_issues`（哪怕只是列在复核区）；pill 映射 `evaluation_incomplete`→"评估不完整"并补 CSS；导出按钮/文案按 `evaluation_status==="complete" && ["QUOTE","PARTIAL_QUOTE"].includes(commercial_disposition)` 区分"报价单(DOCX)"与"分析记录(JSON)"；`web.py:450` 仅在 DOCX 实际生成时 `mark_stage("draft")`。

### C2. `evaluation.py` 金额勾稽比引擎合同更严 —— 分数数量整单误挡（潜在，已复现）

- `rfq_agent/evaluation.py:71-74`：`total == unit_price * quantity` 是**未舍入**的严格相等；
- 而引擎合同是 `pricing.py:246` `line_total = money(q2(unit_price * priced_qty)))` → **HALF_UP 2 位**（policy.py q2）。
- 探针复现：`quantity=2.5, unit_price=1.21` → 引擎正确输出 `line_total=3.03`，validate 判 `amount_consistency:L1` → `_settle` 整单 `evaluation_incomplete`、disposition 锁 PENDING、无 DOCX。
- 触发条件是分数数量（EA/IN/FT 类 RFQ 完全可能，README 明确宣传"可以把自己的 .eml 拖进去"）。六个官方案例数量全为整数，所以 95 个测试全绿也抓不到——与 V2 审查里"潜在缺陷，未触发"的 UOM 问题同类。
- `quote_with_moq` 走 MOQ 数量计价时同理（MOQ 若为小数）。

**建议**（一行 + 一个测试）：`total == q2(unit_price * quantity)`，与引擎舍入合同对齐；再补一个 priced_quantity=2.5 的回归测试。逆命题（真错的金额）仍然会被抓住，只是容差与被审对象一致。

### C3. `terms.py` 合规条款核对文案与新合规段落自相矛盾 —— 同一份文档两页打架

- 本批将 `export_docx.py:269-276` 合规节改为「合规要求待核实……本草案不作任何合规性声明，不得随报价对外发出」，删除了签署栏（远端可见 271-275 行）。
- 但 `rfq_agent/terms.py:88-89` `_compliance` 的判定文案未同步：仍写「草案已留出「合规声明」段落，需签署后随报价发出」；`tests/test_terms.py:48` 还把这句旧文案锁进了断言。
- 位置关系：这份判定文本进入导出 DOCX 的「客户要求与响应」表 → 一旦 C/F 类案例（要求合规声明）出现有价行并产出 DOCX，同一文档里会出现"待签署"与"不作声明"两种矛盾表述。当前因为 C/F 恰好 0 行有价、只产分析记录，所以**矛盾暂未出现在用户可见产物中**——属"设计矛盾已进代码、触发条件未满足"。

**建议**：`_compliance` detail 改为「客户要求合规声明；本草案不作物项分类与出口管制适用性声明，需授权人员核实后方可对外」；同步更新 `test_terms.py:48` 断言。

### C4. 前端 refresh/boot 无错误处理 + GET 端点异常面 —— 一处 5xx 全屏静默

- `static/index.html:408` `Promise.all` 无 catch；448/477/498/671/680/890 各 `refresh()` 调用点均不 catch；`index.html:898-909` boot 中 `await refresh()` 一抛中断，深链 `?task=` 随之失效。
- `web.py:265-277` `/api/fixtures` 在 LOCK 内整跑参考流水线，整个 do_GET 分支无 try/except：`load_case/start_task` 抛 ValueError 或 sqlite OperationalError（见 Y3）直接打断 handler、连接复位，前端连 `{"error":...}` 都拿不到。
- 后果不是崩溃而是**静默空白**：演示现场排障极难。

**建议**：refresh() 分项 try/catch 降级（cases 与 fixtures 互不拖累）；boot 里 refresh 单独兜错后再走深链；`_reference_task_id()` 外包 try/except，失败降级 `reference=false`。

## 🔴 必修（文档，参赛对外资产）

### D1. `RFQ_BUSINESS_AND_IMPLEMENTATION_EVIDENCE.md` 四处"现存缺口"已被本批自己修掉 —— 提交前必须刷新

写作时（3d41501）全部属实，被同批次 6ede16f/8c8674c 追上，文档未随扫尾：

| 位置 | 文档断言 | HEAD 事实 |
|---|---|---|
| EVIDENCE.md:95 | 代码会写"按供应商标准质保""不含出口管制或限制扩散物项" | `export_docx.py:218/271-275` 已删并反向断言（test_export.py:60-62） |
| EVIDENCE.md:92 | `_settle()` 用"无 open review"直接标 complete | `service.py:149-161` 先过 `evaluation.validate()`，活跃门禁拒绝激活（service.py:353-354） |
| EVIDENCE.md:97 | 状态路径未含 `evaluation_incomplete` | `service.py:157` 新增，有 issues 的必经态 |
| EVIDENCE.md:89 | `due_at` 被质量模块称为"交期" | `quality.py:66-72/87-88` 已改"报价回复截止日期"；`models.py:30` 注释同步 |

修法：逐条改写为"已于 6ede16f 修复（见对应测试）"；残留真实缺口请按原来的措辞风格保留（如"完整性校验仅覆盖输出结构，未逐规则 pass/fail/unknown"）。建议文档头注明所审代码基线（3d41501），防止同类时序漂移再发生。

## 🟡 建议修

1. **/api/activate 无现实开关**（web.py:39-44 构造服务不传 policy → 恒 DEFAULT_POLICY → `commercial_approval_enabled=False`）：fail-closed 本身是对的；但 UI 按钮永远报错就欠一个说法。建议按钮旁标"演示策略不可商用激活"，或支持企业批准的 policy 文件加载入口（和 `test_v2_workflow.py:108` 一样走 policy 注入）。
2. **exported 状态覆盖 evaluation_incomplete**（service.py:326-327）：有 evaluation_issues 的任务在 export 后 status 变 "exported"，任务面板失去区分度（`evaluation_status` 仍保留 "incomplete"，激活门禁仍生效）。建议 export 不改 status，或区分 `analysis_exported`。
3. **resolve/openTask/select 竞态**（index.html:667-674 等）：同 revision 双击或并发点选项 → 第二条必中 "Stale revision" 英文红错。onclick 立即 disable + 响应序号丢弃过期结果即可。
4. **/api/preview 解析边界**：`web.py:357` `int(Content-Length)` 在 try 外；`cases.py:147-151`/`web.py:368` 只捕 `(ValueError, OSError)`，损坏 xlsx 的 `zipfile.BadZipFile` 直接断层 + `.pending` 残留。int 包进 try、异常面扩到 Exception→400。
5. **SQLite 读端并发**（web.py:490 ThreadingHTTPServer）：GET 不持 LOCK 与写端并发；`db.py:75-80` 未设 WAL/busy_timeout 且每次 executescript。建议读端进 LOCK 或 connect 设 `busy_timeout`+WAL。
6. **confirm 在途再拖文件会静默丢件**（index.html:875-881 drop 不检查 PV_BUSY/veil；753-760 队列重置 + 784 advanceQueue 推错索引）。uploadFiles 入口 BUSY 拒收并提示。
7. **resetDemo 无 BUSY 防护**（index.html:452-460）：`if (BUSY) return;` 一行。
8. **每请求全量读两个 xlsx 做 SHA-256**（web.py:177-190 → db.py sync）：/api/task 成高频端点后被显著放大；digest 改 size+mtime 或加缓存。
9. **状态图文档漏新状态**：`refer_doc/diagrams/build_page.py:33`、`RFQ_ARCHITECTURE_DIAGRAMS.md:13`、生成的 architecture-diagrams.html 仍写"五种状态"；现为 7 个（+ evaluation_incomplete、active）。
10. **tools/build_expected_buyer_b.py:33-46 仍嵌旧 9 键 POLICY**：重新生成 golden 会把过时策略写进 `_meta.policy`。与 policy.py 对齐或删除已固定化的键。
11. **save_upload 成死代码**（cases.py:119-128，本批被 stage/confirm 流程取代，全库无引用）：删除，避免未来调用绕过预览两段式。
12. **apply_review 不清 evaluation_issues**（service.py:250-275）：resolve 后生成 artifacts 等待再评估前有短窗陈旧值；`task.evaluation_issues = []` 一行。
13. **README.md:4"61 个测试通过"已过时**（现 95，本批未动 README 但它是仓库门面）。
14. **文档组其余**（详见下附子评审）：EVIDENCE.md:71 与前言的"30 天有效期"自相矛盾（当前实现列仍写 30/“仅配置值”，HEAD 无任何 30 天实现，唯 DOCX 标"待企业批准"）；SOLUTION_DESIGN §14（481-512）仍列 7 个已固定化的开关且无实现注记；refer_doc/README.md 文档地图未收录证据文档（全库零入链）；EVIDENCE.md:29 Oracle 概括无出处（违反自设标准）；BACKLOG 的 RULE-008/009、PRICE-006、STATE-001、OUTPUT-007 已实现但仍标 TODO。

## 🟢 可不改

- 前端死代码/遗留面：`ACTIVE_META` 只写不读、render() 里未使用的 `meta`、start() catch 写入 #stages 的错误立即被 finally render() 覆盖；/api/material、/api/import、/api/history、/api/reset、/api/activate、/api/feedback 前端已不调用（保留成 API 面可接受，activate 见 🟡1）。
- 下载 404/409 以原始 JSON 显示新标签页；download 与 /api/task 的 404/400 口径小不一致。
- notice.error 边框色与文字色不统一；dragveil 无 dragend 兜底（可观察）；`_data_trace` 的 `provider.last_query` 进程级共享（并发演示时命中数可能跨任务）。
- 文档措辞：BACKLOG:208-215 SOURCING 增强项放在"当前阻塞"节内；EVIDENCE §1"四种性质标签"未机制化贯穿；E5 链接显示名与 AC 实际标题不一致（"Manage quote, order, and invoice"）。
- `test_export.py` 中被删的两条旧断言对应的行为改造正确（不签署、不断言质保），替换断言到位。

## 明确"未发现"的方面

- **XSS/转义**：所有服务端数据进 innerHTML 处均经 `esc()` 或为数字/固定枚举插值；encodeURIComponent 参数；UTF-8 sidecar 文件读写——未发现注入点。
- **路径穿越/HTTP 头注入**：静态白名单 `/`、`/index.html`；下载 FILE_PATTERN + `Path(item).name` 双防线；上传 `.eml` 后缀校验 + `uuid4().hex` token（32 hex fullmatch）+ `Path(name).name` 归一；响应头值均为常量或正则校验的 ASCII 文件名。
- **`.pending` 隔离声明属实**：`discover` 用 `glob("*.eml")`（非递归）扫 uploads，`.pending` 子目录确实在扫描路径外；令牌校验在 confirm_pending 前置。
- **文档口径与规则编号真实**：文档提到的全部代码级规则名（status_invalid/date_expired/moq_exceeds/condition_mismatch/condition_unknown/stock_insufficient/cost_missing/price_missing/fx_missing/no_price_record/quote_with_moq/accept_alternate_condition）在 pricing.py 逐个真实存在；参数台账（0.20 加价、CNY 1/EUR 7.75/USD 7.1、HALF_UP 先舍成本再加价、行内汇率优先、内部库存优先）与代码逐条吻合。
- **金路验证一致**：B 案例 CLI 端到端 subtotal 386,595.80 与 golden fixture 相符；auto_draft 产生"待复核草案 DOCX"但激活被三重拒绝（complete/"Blocking human review is unresolved"/"Demo pricing policy"）——激活门禁的测试覆盖在这批里做得很扎实。
- **演示重置**：前端"清空当前查看"纯客户端化（不再调 /api/reset）；reset_inbox 幂等；无轮询。

## 仓库卫生（评审过程中发现，与四个提交无关）

本地 F:\Codex\MatrixNEXT 的 main 与 GitHub rfq-agent 的 main 是**两套不相交的提交历史**（同内容、不同 SHA；GitHub 版把 rfq-agent/ 目录内容上提到仓库根）。两侧内容目前逐文件一致，但直接 `git push` 会因不共祖而被拒（需 force 或以远端为准重新克隆）。当前同步方式（手工镜像）建议固化成脚本或改用同一工作副本，避免哪天两边各改一处。

## 如果只修三处

1. **C1**：前端消费 evaluation 字段 + 导出语义对齐（评委演示可直接观察到的硬伤）。
2. **C2**：`total == q2(unit_price * quantity)` 一行 + 分数数量回归测试。
3. **D1**：EVIDENCE.md 四处刷新（顺带 C3 的 terms.py 文案一行），保证参赛文档与 HEAD 一致。
