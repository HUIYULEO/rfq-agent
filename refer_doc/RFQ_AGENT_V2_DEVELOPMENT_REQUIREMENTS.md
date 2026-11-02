# RFQ Agent v2.0 开发需求

> 状态：Development Requirements v2.0  
> 日期：2026-11-02  
> 适用范围：供应商侧 RFQ 接收、CPQ 决策、人工复核、草案生成和持续评测  
> 规范依据：[RFQ_BASELINE_DESIGN.md](./RFQ_BASELINE_DESIGN.md)  
> 本文定位：规定 v2.0 必须实现和验收的产品、数据、状态、规则与评测契约；不规定具体框架、数据库或模型供应商。

## 1. 规范语言与审计基线

本文中的“必须”“禁止”是验收硬约束，“应”表示默认要求，偏离时必须留下书面理由和测试证据。

当前审计表明，现有实现已经具备多载体解析、精确料号查询、库存累计分配、部分状态/有效期/MOQ/件状过滤、Decimal 金额、revision、决策日志和草案输出等基础能力；但这些能力尚未组成可失败关闭的通用 RFQ/CPQ 闭环。v2.0 不以重写现有功能为目标，而是补齐以下系统性缺口：

- 逐规则四值结果以及“预期执行规则是否完整”的独立证明；
- 通用 requirement registry 与行业扩展契约；
- `process_status` 与 `commercial_disposition` 的正交状态；
- 可持久化、可并发校验、可增量恢复的人工中断；
- 事实、规则、策略、计算和产物之间的版本链；
- 通用、行业、企业三层知识边界；
- 以黄金数据和合成反例驱动的持续评测闭环。

实现开始前必须用代码和现有数据重新确认审计事实；本文不把“审计时存在”视为未经验证即可标记完成。

## 2. 目标与非目标

### 2.1 目标

v2.0 必须实现以下目标：

1. 将来自邮件、表格、PDF、API 或人工录入的 RFQ 统一为带字段级证据的通用模型。
2. 对内部库存、成本和上游供应商候选执行确定性资格判断，并明确区分 `pass`、`fail`、`unknown`、`not_applicable`。
3. 证明每次评估不仅“执行了计划内规则”，而且计划本身覆盖了当时全部应执行规则。
4. 只对完整评估后的合格候选执行确定性排序与定价；任何报价数字均可复算。
5. 将处理进度与商业结论分开，防止“流程结束”被误解释为“可以出报价”。
6. 在信息不足、策略冲突或需要授权时可靠中断，人工处理后从有效检查点恢复，而不是丢失上下文或重复副作用。
7. 按商业处置生成匹配的报价草案、部分报价、供应商询价任务、客户澄清清单或无法报价回复，并阻止陈旧产物继续使用。
8. 建立三层知识体系和数据账本，使新增行业或企业规则不需要修改通用状态机与金额模型。
9. 用离线黄金集、合成边界集和线上反馈形成可回归的 eval 闭环。

### 2.2 非目标

v2.0 不包含以下范围：

- 自动发送具有法律约束力的正式报价或合同；首版只生成明确标记的草案或任务清单。
- 让 LLM 生成库存、成本、汇率、供应商状态、交期或任何价格事实。
- 用向量检索结果直接覆盖结构化事实、资格结论或定价规则。
- 一开始建设微服务、消息队列、通用 BPM 平台或全行业本体。
- 默认实现模糊料号自动替换、自动拆单、多供应商组合优化、动态博弈定价或自动联系外部供应商。
- 以单一航空案例、单一企业字段或单一演示策略定义通用产品边界。
- 把模型解释文本、系统自身历史输出或人工未确认的推断当作黄金答案。

## 3. 权威来源层级

权威性必须按信息职责判断，不能用一个来源覆盖它无权定义的字段。实现必须为每个关键字段和决策保存 `authority_class`、`source_ref`、版本或时间以及证据位置。

| 层级 | 权威来源 | 可以决定 | 不可以决定 |
|---|---|---|---|
| A1 | 原始 RFQ、附件、客户后续确认 | 客户身份、请求物、数量、单位、交付要求、明确商业条款 | 内部库存、成本、批准供应商状态、最终报价 |
| A2 | 企业事实系统的不可变快照 | 库存、成本、供应商报价、汇率、客户/供应商状态、历史承诺 | 改写客户原始要求或绕过审批规则 |
| A3 | 经批准的企业规则与策略目录 | 资格规则、授权门槛、选候选策略、加价与舍入、允许产物 | 伪造事实或抹去原始证据 |
| A4 | 经批准的行业扩展与标准映射 | 行业 requirement 类型、术语、单位、证据要求和行业规则 | 静默放宽核心或企业硬规则 |
| A5 | 通用知识层 | 通用 RFQ/CPQ 概念、字段解释、问题模板和规则模板 | 直接产生企业事实、资格或金额 |
| A6 | 检索片段、模型推断和自由文本说明 | 辅助解释、生成待确认问题、提出候选映射 | 作为事实快照、规则结果或定价输入 |

冲突处理必须遵守以下要求：

- 同一职责域内，高层级且仍有效的来源优先；冲突双方都必须保留，不能覆盖原值。
- 客户要求与企业政策冲突时，不把客户要求“纠正”为企业政策；系统必须保留客户要求，并以规则结果拒绝、转复核或请求变更。
- 企业规则只能引用同版本或明确兼容的行业/通用定义。
- A6 内容若要影响资格或价格，必须先经过负责人批准，转成 A2 事实或 A3/A4 结构化版本。
- 来源缺失、过期或冲突时使用 `unknown` 或结构化 issue，禁止选择“看起来最合理”的值。

## 4. 通用 supplier-side RFQ/CPQ SOP

所有渠道和行业必须遵守同一主流程。扩展包可以增加检查，不能跳过步骤或改变核心金额契约。

| 阶段 | 必须输入 | 必须动作 | 必须输出与退出条件 |
|---|---|---|---|
| 1. Intake | 原始文件、渠道元数据 | 生成 `task_id`，计算文件哈希，关联附件，保存原始字节和接收时间 | 不可变 source record；缺文件不得进入解析 |
| 2. Parse | source record、解析器版本 | 提取单据、行项目和条款，保留原值、规范值、位置、置信度 | 解析结果和问题；不得把缺失值补成事实 |
| 3. Normalize | 解析结果、通用 schema、已加载扩展 | 统一日期、Decimal、单位、标识符和 requirement 表示 | 新 RFQ `revision`；未注册要求仍被保留 |
| 4. Validate | RFQ revision、字段契约 | 检查必填、类型、重复、冲突、单位和证据覆盖 | validation results；blocking unknown 形成 review item |
| 5. Snapshot | 已批准的数据连接器 | 冻结库存、成本、供应商报价、汇率和状态 | `fact_snapshot_id`；后续刷新必须产生新快照 |
| 6. Discover | RFQ revision、事实快照 | 精确身份查询内部与上游供应商候选；模糊结果只作人工候选 | 冻结 candidate set；不得生成不存在的候选 |
| 7. Plan | RFQ、requirements、candidate set、规则/策略/扩展版本 | 确定性重建 expected rule-instance set，并生成评估计划 | 通过 plan completeness 校验后才可执行 |
| 8. Evaluate | 已验证计划、事实快照 | 每个 rule instance 恰好生成一个四值结果 | `evaluation=complete` 或 `incomplete`；不完整时失败关闭 |
| 9. Aggregate | 完整规则结果 | 聚合候选资格、行状态和未决问题 | 只有 `eligible` 候选可进入排序/定价 |
| 10. Configure/Price | 合格候选、版本化策略 | 确定性选候选、分配数量、换算、加价、费用和舍入 | 完整 calculation trace；缺事实时中断 |
| 11. Decide | 行结果、review actions | 按规则推导 `commercial_disposition` | 处置与理由可重建，不由 `process_status` 推断 |
| 12. HIL | review item、检查点 | 中断、展示影响、接收授权动作、增加 revision、局部重算 | 所有 blocking item 被处置，或产生保守处置 |
| 13. Artifact | 已结算处置、当前版本输入 | 校验门禁并生成允许的产物 | 当前 `ArtifactManifest`；陈旧产物禁止下载/发送 |
| 14. Close/Eval | 全链路账本 | 保存结果、人工反馈、指标和失败分类 | 可回放审计记录和 eval 样本，不自动污染黄金集 |

任何阶段重试必须幂等。网络或临时错误可以按配置限次重试；参数、权限、认证、契约和业务校验错误必须立即停止并结构化报告。

## 5. 核心状态与对象契约

### 5.1 任务版本与不可变输入

任务至少必须保存：

```text
task_id
rfq_revision
core_contract_version
extension_versions[]
rule_catalog_version
policy_version
fact_snapshot_id
candidate_set_id
evaluation_plan_id
evaluation_status
process_status
commercial_disposition
current_artifact_manifest_id
```

以下变化必须创建新 revision 或新版本引用，并使依赖它的评估、价格和产物失效：客户要求、规范值、候选集合、事实快照、规则目录、策略、扩展版本、人工例外和计算配置。

### 5.2 `process_status` 与 `commercial_disposition`

两个状态必须独立持久化，禁止使用一个字段兼任。

`process_status` 表示技术处理进度：

```text
received → parsed → validated → evaluated ─┬→ awaiting_review → settled
                                           └→ settled
settled → artifact_ready → completed
任一处理中状态 → failed
```

无商业文件的合法处置可从 `settled` 直接进入 `completed`。`failed` 只表示技术处理失败，不等于 `cannot_quote`。

`commercial_disposition` 表示商业结论：

```text
undecided
quote_draft
partial_quote
supplier_inquiry_required
cannot_quote
needs_customer_clarification
cancelled
```

必须满足以下不变量：

- `process_status=evaluated` 不表示可报价。
- `process_status=settled` 要求 `evaluation_status=complete` 且 `commercial_disposition != undecided`。
- `evaluation_status=incomplete` 时禁止进入 `settled`、`artifact_ready`，也禁止生成报价类产物。
- `artifact_ready` 要求存在当前、非 stale、类型与处置一致的 `ArtifactManifest`。
- review item 全部 answered 只表示人工任务已处理，不能自动推导 `quote_draft`。
- `failed`、`awaiting_review`、`settled` 和 `artifact_ready` 的转换必须记录事件、操作者或系统主体、时间和先决条件结果。

### 5.3 处置推导与产物门禁

| 条件 | 基准处置 | 允许产物 |
|---|---|---|
| 所有应报价行均为 `priced`，无 blocking unknown | `quote_draft` | 报价草案、决策记录 |
| 同时有 `priced` 和经授权 `not_quoted` | `partial_quote` | 明示未报价行的部分报价、决策记录 |
| 必须取得新的上游价格或数量承诺 | `supplier_inquiry_required` | 供应商询价任务/清单；禁止报价单 |
| 经授权确认不报价 | `cannot_quote` | 无法报价回复草案；禁止报价单 |
| 必须由客户补字段或确认冲突 | `needs_customer_clarification` | 客户问题清单；禁止报价单 |
| 授权用户终止任务 | `cancelled` | 审计记录；禁止商业报价文件 |

若多个条件同时成立，默认优先级为：

```text
cancelled
> needs_customer_clarification
> supplier_inquiry_required
> cannot_quote
> partial_quote
> quote_draft
```

人工只能在规则目录声明的允许范围内选择更保守处置，必须记录理由和权限。产物服务必须独立复核处置、evaluation 完整性、revision 和 manifest 依赖，不能信任 Agent 传入的文件类型。

## 6. P0：EvaluationPlan 必须可重建且失败关闭

本节是 v2.0 的最高优先级契约。`EvaluationPlan` 不能以自身内容证明自身完整。

### 6.1 预期规则集的确定性重建

服务端必须由以下不可变输入确定性计算 `ExpectedRuleInstanceSet`：

```text
core contract version
+ RFQ revision（含 global/line requirements）
+ fact snapshot（含冻结 candidate set）
+ rule catalog version
+ policy version
+ extension versions
= ExpectedRuleInstanceSet
```

每个实例必须有稳定的 `rule_instance_key`，至少覆盖：

```text
scope + rule_id + rule_version + line_id + candidate_id
+ requirement_id + fact_snapshot_id + applicability_key
```

重建算法必须包含：

- 所有核心常驻规则；
- 规则目录中适用条件为真的条件规则；
- 每个候选、行项目和整单 scope 所需的规则实例；
- 每个已注册 requirement 的独立 `REQUIREMENT.EVALUATE` 实例；
- 每个未注册 requirement 的 blocking unknown 实例，确保未知要求不会因缺 evaluator 而消失；
- 全局 requirement 按扩展契约继承到每行后产生的实例；
- 候选集合、事实快照或 requirements 变化后产生的新实例集合。

相同规范化输入与版本必须生成字节级可比较的有序 key 集和相同 plan hash；排序不得依赖数据库返回顺序或模型输出顺序。

### 6.2 计划完整性校验

持久化的 `EvaluationPlan` 必须与重新构建的 `ExpectedRuleInstanceSet` 做集合等价校验。以下任一情况都必须使 `evaluation_status=incomplete`：

- 缺少 expected rule instance；
- 同一 key 重复；
- 出现无法由当前版本输入解释的计划项；
- rule id、版本、scope、candidate、requirement 或事实快照引用不一致；
- plan hash、输入版本或适用条件证据不一致；
- 未注册 requirement 没有对应 blocking unknown 计划项。

只有计划完整性通过后才允许执行规则。

### 6.3 结果完整性与聚合

每个 expected rule instance 必须且只能产生一条 `RuleResult`。结果必须引用相同的 key、输入 refs、规则版本和事实快照。

以下任一情况同样必须使 `evaluation_status=incomplete`：结果缺失、重复、计划外结果、版本错配、无法解析的 outcome、规则执行异常或结果引用陈旧输入。

完整评估后才允许聚合：

- 任一 blocking `fail` → `rejected`；
- 无 blocking fail，但存在 blocking `unknown` → `needs_review`；
- 所有 blocking 结果均为 `pass` 或经适用条件证明的 `not_applicable` → `eligible`。

`not_applicable` 必须保存适用条件和输入证据。缺字段不能通过 `not_applicable` 掩盖。`evaluation_status=incomplete` 时不得产生 `eligible`，不得定价，不得进入 `settled` 或 `artifact_ready`。

## 7. HIL interruption / resume

### 7.1 中断对象

每个需要人工处理的问题必须持久化为 `ReviewItem`，至少包含：

```text
review_item_id, task_id, base_revision, stage, checkpoint_id
scope, line_id, candidate_id, requirement_id
reason_code, severity, blocking
question, allowed_actions[], required_payload_schema
input_refs[], affected_outputs[]
status, created_at, answered_at, actor
```

问题必须指出缺什么、证据在哪里、对资格/金额/处置有什么影响；禁止只显示“需要人工复核”。多个问题可以批量展示，但每个问题必须独立记录结果。

### 7.2 检查点与恢复

中断前必须提交持久化检查点，包含当前 revision、各版本引用、已完成阶段、阶段输出 hash 和未决 review items。恢复时服务端必须：

1. 校验 `task_id`、`base_revision`、review item 状态和幂等键；
2. 校验动作属于 `allowed_actions` 且 payload 符合 schema；
3. 记录 actor、角色、时间、理由和 before/after；
4. 创建新 revision 或新的事实/策略版本引用；
5. 计算受影响依赖图，仅复用仍与输入 hash 一致的结果；
6. 重新构建 ExpectedRuleInstanceSet，而不是在旧计划上手工追加结果；
7. 使受影响价格和 ArtifactManifest 标记 stale；
8. 从最早受影响的确定性阶段恢复，并重新执行门禁。

陈旧 revision、重复回答、已关闭 review item、越权动作和未知 action 必须被拒绝且不产生部分副作用。

### 7.3 人工动作语义

首版至少支持：

- `provide_missing_fact`：提供可验证事实或选择已有来源；不能直接输入“通过”。
- `confirm_mapping`：确认别名、单位或字段映射，并保存授权证据。
- `request_supplier_quote`：进入供应商询价处置，不得生成报价单。
- `accept_exception`：只能覆盖规则声明为 `role_required` 的结果；原始 fail/unknown 永久保留，并新增授权记录。
- `change_requirement`：保留客户原要求、before/after、依据，增加 RFQ revision。
- `cannot_quote`：产生无法报价处置和理由。
- `cancel`：由授权角色终止任务并保留审计记录。

规则目录必须声明 `override_policy=non_overridable | role_required`、允许角色、有效范围和是否需要二次审批。核心身份、正数金额、计算可复算和证据完整性等不可覆盖规则必须显式列入目录。

## 8. 确定性候选、分配与定价边界

### 8.1 Agent 与确定性服务边界

LLM/Agent 可以识别意图、选择已定义业务动作、生成解释和提出问题；不得：

- 构造或修改价格、成本、库存、汇率、数量、供应商状态和有效期；
- 自行放宽 blocking requirement；
- 选择计划外规则结果或直接设置 `eligible`；
- 绕过 revision、HIL、evaluation 或 artifact 门禁；
- 将检索文本作为计算值传给定价引擎。

### 8.2 候选与数量分配

- 默认精确身份匹配；别名、替代品和模糊匹配只能形成 review item。
- 候选必须绑定事实快照、来源、数量及 UOM、MOQ、成本/价格币种、有效期、交期依据和证据。
- “现货”但无数量仍是 `available_quantity=null`，结果为 blocking unknown。
- 累计分配按 `inventory_pool_id + quantity_uom` 记账；不得因重复 RFQ 行重复使用同一库存。
- 缺少显式单位换算时不得跨单位扣减或比较。
- 首版默认单候选满足一行且不超额采购；拆单或多来源组合属于 P2，除非企业策略显式启用并提供独立验收。
- 候选排序只在 `eligible` 集合内执行，排序策略必须版本化并以稳定候选 ID 最终决胜。

### 8.3 定价输入与计算轨迹

定价引擎只接受通过 schema 校验的 `PricingInput`：

```text
selected candidate and allocation
cost amount + cost currency + cost source
quote currency
structured FX {from,to,value,as_of,source_ref}
markup/discount policy
fee/tax/freight components and inclusion flags
calculation order
rounding mode and scale for every step
quantity and UOM
all referenced versions
```

金额必须使用 Decimal 字符串。缺价格为 blocking `unknown`，非正价格为 blocking `fail`；缺币种或所需汇率为 `unknown`。禁止用 0、1、当前日期或模型估计填补未知值。

每一步必须保存输入、公式、未舍入结果、舍入规则、舍入后结果和输出币种。`unit_price × quoted_quantity`、行金额、已报价小计和费用组成必须可由独立实现复算。未报价字段必须为 `null`，不得填 0；部分报价的汇总只能称为“已报价项目小计”。

### 8.4 产物可追溯性

每个产物必须有 `ArtifactManifest`：

```text
artifact_id, artifact_type, content_hash
task_id, rfq_revision, fact_snapshot_id, candidate_set_id
evaluation_plan_id, rule_catalog_version, policy_version
extension_versions, pricing_trace_ids, disposition
generated_at, status=current|stale|revoked
```

任何依赖变化时旧 manifest 必须原子标记 stale。下载、预览、发送和复制当前链接时必须再次校验 manifest；仅在生成时校验不合格。

## 9. 三层知识体系

三层知识必须物理或逻辑分区、独立版本化，并通过受控接口组合。

### 9.1 通用层 `common`

包含行业无关的 RFQ/CPQ schema、核心状态机、核心规则模板、四值语义、金额契约、通用术语和问题模板。该层不得包含航空件状枚举、某企业加价率、客户名单或真实价格。

### 9.2 行业层 `industry`

以扩展包提供行业 requirement 类型、operator、单位、normalizer、rule evaluator、术语、证据要求和 fixtures。扩展包必须声明：

```text
extension_id/version
compatible core contract versions
requirement JSON type/cardinality/unit/operators
normalizer output schema
rule evaluator mapping
evidence requirements
conflict priority and migration notes
positive/negative/conformance fixtures
```

行业扩展只能增加或收紧核心规则。冲突时取更保守结果并产生配置错误，禁止静默替换。

### 9.3 企业层 `enterprise`

包含企业数据连接器、批准供应商/客户策略、加价与舍入、授权角色、文档模板、字段映射和私有术语。事实数据与规则必须分开：实时或历史业务事实进入不可变快照，企业政策进入版本化规则/策略目录。

优先级为企业批准策略约束行业规则，行业规则约束通用模板；“更高层”只能在自身权限内收紧或显式覆盖允许覆盖的项目，不能改写下层证据。

### 9.4 检索与知识晋升

- 检索必须返回 layer、document/version、位置、有效期和权限标签。
- 检索文本只可用于解释或形成待确认事项；不得直接写入 `FactSnapshot`、`RuleResult` 或 `PricingInput`。
- 将知识晋升为规则或企业事实必须经过 schema 校验、负责人审批、版本发布和回归测试。
- RAG 无结果、低置信度或不同层冲突时必须显式报告，不能由模型补齐。
- 每次决策必须记录实际使用的结构化版本；仅记录“检索过某文档”不足以作为计算证据。

## 10. 数据账本

v2.0 必须提供 append-only 的逻辑账本。存储技术可选，但不允许用覆盖更新抹掉历史。

| 账本 | 必须记录 |
|---|---|
| Source Ledger | 原始文件 hash、渠道、接收时间、附件关系、访问控制 |
| Evidence Ledger | 字段原值、规范值、文件位置、解析器版本、置信度、冲突关系 |
| Revision Ledger | RFQ before/after、变更原因、actor、父 revision |
| Fact Ledger | snapshot ID、数据来源、query/as-of、候选集合、每条事实证据 |
| Rule Ledger | rule/extension/policy 版本、适用条件、真值表、覆盖策略、负责人 |
| Evaluation Ledger | expected set hash、plan、每条结果、完整性校验和聚合结果 |
| Allocation Ledger | 库存池、数量、UOM、分配顺序、释放/失效原因 |
| Pricing Ledger | 输入版本、公式步骤、汇率、费用、舍入和最终金额 |
| HIL Ledger | review item、允许动作、回答、权限、before/after、幂等键 |
| Disposition Ledger | 候选处置、最终处置、推导规则、人工保守选择和理由 |
| Artifact Ledger | manifest、hash、依赖、current/stale/revoked 转换和访问记录 |
| Eval Ledger | 数据集版本、运行配置、指标、失败分类、回归比较和发布结论 |

所有账本记录必须可通过 `task_id` 和 revision 关联。敏感业务数据必须按企业权限过滤；日志不得泄露凭据、完整供应商表或不属于当前任务的数据。

## 11. Eval 闭环

### 11.1 数据集分层

至少维护四类评测集：

1. `core_synthetic`：缺值、冲突、边界日期、UOM、汇率、库存累计和计划缺失等通用反例。
2. `industry_conformance`：每个行业扩展的正例、反例、未注册 requirement 和证据要求。
3. `enterprise_golden`：经业务人员确认的真实脱敏任务、候选、定价和处置。
4. `shadow_feedback`：线上或演示运行中收集的失败案例；未经审核不能直接进入 golden。

训练、调试和发布回归集必须分开。生产提示词、规则或代码不得包含固定回归答案。

### 11.2 指标

P0 发布门至少包含：

- 关键字段及证据定位的黄金集准确率；
- requirement 保留率 100%；
- expected rule-instance set 覆盖和唯一性 100%；
- blocking unknown 静默通过数为 0；
- 合格/拒绝/复核聚合与黄金结果一致率；
- 已报价金额独立复算一致率 100%；
- disposition 与允许产物违规数为 0；
- stale artifact 被下载或发送数为 0；
- stale revision、重复 HIL 回答和越权例外拒绝率 100%；
- 相同规范化输入与版本重复运行的结构化结果和金额一致率 100%。

解析类概率模型可以另设召回率和人工复核率，但不得用平均准确率抵消金额、门禁或证据链的硬失败。

### 11.3 反馈闭环

每次人工修订和失败必须先分类为解析、规范化、事实、规则、策略、权限、定价、处置、产物或知识问题。修复必须落到所属层：

- 解析缺陷更新解析器和对应 fixture；
- 行业语义缺陷更新扩展版本；
- 企业政策变化发布新策略版本；
- 单次事实纠正创建新快照或 revision；
- 模型解释问题不得通过修改确定性规则“顺便修好”。

进入发布候选前必须运行受影响单元集、core synthetic、全部已启用扩展 conformance 和企业黄金回归。发布后保留基线指标，指标下降必须能定位到版本和失败样本。

## 12. 优先级与交付要求

### 12.1 P0：可失败关闭的业务闭环

| ID | 必须交付 | 验收证据 |
|---|---|---|
| V2-P0-001 | 通用 RFQ、requirement、candidate、rule result、pricing trace schema | schema 校验和迁移测试 |
| V2-P0-002 | 不可变 FactSnapshot 与 candidate set | 快照 hash、as-of 和来源可查询 |
| V2-P0-003 | 规则目录和逐规则四值真值表 | 核心规则正反例全部通过 |
| V2-P0-004 | ExpectedRuleInstanceSet 重建器、plan compiler 和独立 completeness validator | 漏项、重复、错版本、计划外项全部得到 `incomplete` |
| V2-P0-005 | `evaluation_status` 聚合与失败关闭门禁 | incomplete 无法定价、settled 或 artifact_ready |
| V2-P0-006 | `process_status` / `commercial_disposition` 分离及迁移 | 状态转换契约测试 |
| V2-P0-007 | ReviewItem、持久化 checkpoint、revision 和幂等 resume | 中断重启、陈旧回答、重复回答测试 |
| V2-P0-008 | 确定性候选排序、库存分配和 Decimal 定价轨迹 | 独立复算与重复运行一致 |
| V2-P0-009 | disposition 推导、artifact gate 和 ArtifactManifest | 不相符产物与 stale 下载被拒绝 |
| V2-P0-010 | 三层知识命名空间、权限与“检索不得入价”边界 | 跨层冲突和低置信度测试 |
| V2-P0-011 | append-only 核心账本和任务回放 API | 任一报价行可回溯完整版本链 |
| V2-P0-012 | core synthetic 与企业黄金回归门 | 第 11.2 节硬指标全部达标 |

### 12.2 P1：可扩展和可运营

| ID | 应交付 | 验收证据 |
|---|---|---|
| V2-P1-001 | requirement registry 和行业扩展 SDK/conformance runner | 新增一个行业 fixture 不修改核心状态机/金额代码 |
| V2-P1-002 | 全局 requirement 继承、行级收紧和显式 override | 继承/冲突组合测试 |
| V2-P1-003 | 依赖图驱动的局部重算 | 修改单行不重跑无关解析且结果与全量重算一致 |
| V2-P1-004 | 企业知识晋升审批和版本发布流程 | 草稿不能影响生产评估，发布后有审计链 |
| V2-P1-005 | eval dashboard、失败聚类和版本对比 | 可按解析器/规则/策略/扩展定位回归 |
| V2-P1-006 | 结构化供应商询价与客户澄清模板 | 回复可关联原 review item 并创建新 revision |
| V2-P1-007 | 可观测性、限次重试、超时和成本记录 | 故障注入不产生重复业务副作用 |

### 12.3 P2：高级能力

| ID | 可选交付 | 前置条件 |
|---|---|---|
| V2-P2-001 | 别名/模糊候选检索 | 仅人工确认，不降低 exact-match 门禁 |
| V2-P2-002 | 拆单与多来源组合优化 | 分配账本、费用模型和组合黄金集已具备 |
| V2-P2-003 | 批量 RFQ、异步队列和水平扩展 | 单任务幂等、恢复和观测指标稳定 |
| V2-P2-004 | 企业策略仿真与 what-if | 与正式事实/结果隔离，不生成当前产物 |
| V2-P2-005 | 受控外部供应商通信 | 发送审批、身份、内容审计和回执关联完成 |

P0 未通过前，P1/P2 功能不得通过降级门禁或扩大自动化范围来“弥补”核心闭环。

## 13. 端到端验收场景

以下场景必须自动化，且断言账本、状态和禁止行为，不只检查页面文字。

| ID | Given / When | Then |
|---|---|---|
| AC-01 正常报价 | 完整 RFQ、单币种、足量库存、全部规则通过 | evaluation complete；全部行 priced；处置 quote_draft；金额可复算 |
| AC-02 缺单价 | 候选没有单价 | PRICE.REQUIRED=blocking unknown；不得填 0；进入复核或供应商询价 |
| AC-03 非正价格 | 候选价格为 0 或负数 | blocking fail；候选 rejected；不能定价 |
| AC-04 缺汇率 | 成本币种与报价币种不同且 FX 缺失/方向错误 | blocking unknown；不得默认 1；无报价产物 |
| AC-05 “现货”无数量 | 供应商只声明现货 | available quantity 仍为 null；不得判足量 |
| AC-06 MOQ | 请求量低于 MOQ 且不允许超额采购 | 候选被拒或复核；未经授权不得增加报价量 |
| AC-07 累计库存 | 两行引用同一 inventory pool | 累计分配不超过库存；顺序由稳定策略决定 |
| AC-08 UOM | 请求与候选单位不同且无换算 | blocking unknown；不得跨单位扣减或定价 |
| AC-09 交期 | 客户有交付要求但候选只有无起算点裸天数 | DELIVERY.FEASIBLE=unknown；回复截止不得代替交付截止 |
| AC-10 价格状态冲突 | 状态无效但日期仍有效，或状态有效但日期过期 | 状态与日期分别出结果；任一 blocking fail 均拒绝 |
| AC-11 未注册要求 | RFQ 含扩展未注册 key | 原文和证据保留；expected set 含 blocking unknown；不得静默丢弃 |
| AC-12 计划漏规则 | 持久化计划漏掉一个按目录和快照应生成的规则实例 | 重建集合不相等；evaluation=incomplete；禁止 eligible、settled、artifact_ready |
| AC-13 重复/错版结果 | 同一实例产生两条结果或引用旧 fact snapshot | evaluation=incomplete；不得聚合或定价 |
| AC-14 计划确定性 | 相同 RFQ、requirements、快照和版本重复构建计划 | rule-instance keys 和 plan hash 完全相同 |
| AC-15 HIL 恢复 | 在 blocking unknown 中断后提供有效事实 | 新 revision/快照；重建计划；只复用 hash 未变结果；旧产物 stale |
| AC-16 stale HIL | 用旧 revision 回答或重复提交同幂等键 | 请求被拒或返回原结果；无重复授权和副作用 |
| AC-17 不可覆盖规则 | 非授权角色接受例外，或尝试覆盖 non_overridable | 动作被拒；原规则结果保持 |
| AC-18 部分报价 | 部分行 priced，其他行经授权 not_quoted | 处置 partial_quote；仅汇总已报价行并显式列出未报价行 |
| AC-19 供应商询价 | 需要上游新价格 | 只生成 supplier inquiry；报价导出接口拒绝 |
| AC-20 无法报价/澄清 | 人工确认不报价或必须由客户补充 | 生成对应回复/问题清单；不得生成报价单 |
| AC-21 处置冲突 | 同时满足多个处置条件 | 按优先级得到唯一结果；人工保守选择有权限和理由 |
| AC-22 产物失效 | 产物生成后修改事实、规则、策略或 requirement | manifest 立即 stale；旧下载/发送请求被拒绝 |
| AC-23 知识越界 | RAG 返回一个价格或放宽规则建议 | 只可展示为说明/待确认；FactSnapshot、RuleResult、PricingInput 不变化 |
| AC-24 扩展冲突 | 行业规则试图放宽核心 blocking 规则 | 取更保守结果并报配置冲突；不得静默替换 |
| AC-25 可复现 | 相同规范化输入、快照和全部版本运行两次 | 结构化资格、处置和所有 Decimal 金额一致 |
| AC-26 技术失败 | 解析器或规则执行器抛出异常 | process failed 或 evaluation incomplete；不得映射为 cannot_quote 或生成商业产物 |

## 14. v2.0 完成定义

只有同时满足以下条件，v2.0 才能标记完成：

- P0 交付项全部实现并有自动化证据；
- 第 13 节场景全部通过，且禁止行为有明确断言；
- 计划完整性可由规则目录、事实快照和 requirements 独立重建证明；
- 任一 `evaluation_status=incomplete` 都无法到达 `settled` 或 `artifact_ready`；
- 任一报价数字都能从账本回溯到原始证据、事实快照、规则/策略/扩展版本和逐步公式；
- HIL 在进程重启、陈旧 revision、重复提交和权限不足情况下保持一致；
- `process_status`、`commercial_disposition` 和产物类型不存在非法组合；
- 新增一个行业扩展无需修改通用状态机和金额模型；
- common、industry、enterprise 三层知识的权限、版本和晋升路径通过测试；
- eval 报告包含数据集版本、运行配置、硬指标、失败样本和发布结论。

任何未满足项必须保持为明确的 P0/P1/P2 backlog，不得以演示成功、人工目测或模型解释替代验收。
