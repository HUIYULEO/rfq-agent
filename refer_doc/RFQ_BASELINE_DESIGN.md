# RFQ 行业无关基准设计

> 状态：Baseline v1.0  
> 日期：2026-11-02  
> 适用范围：供应商侧 RFQ 接收、候选匹配、报价建议、人工复核与草案输出  
> 设计原则：通用核心稳定，行业语义以配置化扩展包接入；领域知识不能绕过确定性规则和证据要求。

## 1. 目标

本基准定义一套不绑定航空、制造、电子元器件或其他单一行业的 RFQ 核心契约。任何行业案例首先必须通过本基准，再按需要加载领域扩展。

基准必须回答五个问题：

1. 客户要买什么、买多少、何时需要，原始证据在哪里？
2. 哪些内部或外部候选可能满足需求？
3. 每个候选逐条通过、失败、未知或不适用哪些要求？
4. 报价数字由什么事实、规则和配置计算得出？
5. 系统下一步是可出草案、待补信息、转人工询价、拒绝还是取消，而不是笼统地“完成”。

## 2. 分层边界

```text
原始 RFQ 与附件
→ 解析与标准化
→ 通用 RFQ Schema
→ 数据质量检查
→ 候选事实查询
→ 规则目录逐条评估
→ 候选排序与确定性计算
→ 人工决策门
→ 与商业处置相符的产物
```

职责边界：

- LLM/Agent：识别意图、选择业务动作、解释结果、提出具体问题；不得生成价格事实或自行改写硬约束。
- 解析器：读取载体并保留原值、规范值、位置和置信度；不得把缺失值补成业务事实。
- 数据服务：提供库存、成本、供应商报价、客户和供应商状态等事实快照。
- 规则引擎：逐条输出确定性的资格结论；未知不能静默视为通过。
- 定价引擎：只对合格候选计算 Decimal 金额，并保存完整计算轨迹。
- 知识层：提供术语、政策和规则来源；检索结果不能直接成为库存、价格或资格结论。

## 3. 通用领域模型

### 3.1 RFQ 单据

```json
{
  "rfq_id": "",
  "revision": 1,
  "buyer": {"id": null, "name": ""},
  "received_at": null,
  "evaluation_at": "",
  "response_due_at": null,
  "requested_currency": null,
  "delivery_destination": null,
  "commercial_terms": {},
  "global_requirements": [],
  "lines": [],
  "source_documents": [],
  "issues": []
}
```

`response_due_at` 是回复截止时间，不等于交付截止时间。缺少币种、目的地或贸易条款时保留 `null` 并按策略转复核，不做行业默认猜测。

### 3.2 行项目

```json
{
  "line_id": "L001",
  "item_identifier": {"type": "part_number", "value": ""},
  "description": "",
  "requested_quantity": "1",
  "unit_of_measure": "EA",
  "delivery_due_at": null,
  "requirements": [],
  "evidence": []
}
```

通用核心只固定身份、描述、数量、单位和交付时间。行业条件统一表示为类型化要求，而不是不断向核心表增加行业专属列：

```json
{
  "requirement_id": "REQ-L001-01",
  "key": "condition",
  "operator": "in",
  "expected": ["FN"],
  "severity": "blocking",
  "source": {"document_id": "DOC-1", "location": "sheet1!G2", "raw_value": "FN"}
}
```

`key` 由扩展包注册。航空可以注册 `condition`、`certificate`、`traceability`；机械加工可以注册 `material`、`tolerance`、`surface_finish`。未注册的要求必须保留并转人工，不能丢弃。

### 3.3 候选事实

```json
{
  "candidate_id": "",
  "line_id": "L001",
  "source_type": "internal | supplier",
  "source_ref": "",
  "identity": {},
  "available_quantity": null,
  "moq": null,
  "unit_cost": null,
  "currency": null,
  "exchange_rate": null,
  "valid_from": null,
  "valid_until": null,
  "lead_time_days": null,
  "attributes": {},
  "evidence": []
}
```

候选中的 `null` 表示未知，不能用 `0`、`1` 或当前日期替代。供应商声称“现货”但没有数量时，`available_quantity` 仍为 `null`。

### 3.4 规则结果

每个适用规则必须产生一条独立记录：

```json
{
  "rule_id": "PRICE.REQUIRED",
  "rule_version": "1.0.0",
  "outcome": "pass | fail | unknown | not_applicable",
  "severity": "blocking | warning | info",
  "input_refs": [],
  "reason_code": "price_missing",
  "message": "供应商单价缺失，不能自动定价"
}
```

候选资格由规则结果聚合：

- 任一 blocking `fail` → `rejected`；
- 无 blocking fail，但存在 blocking `unknown` → `needs_review`；
- 所有 blocking 规则为 `pass` 或 `not_applicable` → `eligible`。

`not_applicable` 只能由规则适用条件明确产生，不能用来掩盖缺失数据。

### 3.5 报价决策与计算轨迹

```json
{
  "line_id": "L001",
  "selected_candidate_id": null,
  "eligibility": "priced | needs_review | not_quoted",
  "quoted_quantity": null,
  "unit_price": null,
  "line_total": null,
  "calculation": {
    "cost_basis": null,
    "markup_rate": null,
    "rounding": null,
    "formula": null
  },
  "rule_results": [],
  "warnings": []
}
```

金额字段使用 Decimal 字符串。未报价必须为 `null`，不得填 `0`。整单只能称为“已报价项目小计”，直到所有应报价行均已完成且费用口径明确。

## 4. 通用规则基线

所有行业都必须实现以下规则；扩展包只能增加或收紧规则，不能绕过它们。

| 规则 | 基准行为 |
|---|---|
| `IDENTITY.EXACT_MATCH` | 默认精确匹配；别名、替代品或模糊匹配必须转复核 |
| `QUANTITY.POSITIVE` | 请求量、报价量必须为有限正数 |
| `UOM.COMPATIBLE` | 单位相同或存在显式换算；未知换算转复核 |
| `PRICE.REQUIRED` | 单价缺失或非正数时拒绝自动定价 |
| `CURRENCY.REQUIRED` | 非基准币种必须有明确汇率和方向；不得默认汇率为 1 |
| `PRICE.STATUS_AND_DATE` | 状态有效且评估日在有效期内；两者分别记录结果 |
| `AVAILABILITY.SUFFICIENT` | 已知库存不足则拒绝；数量未知则转复核 |
| `MOQ.SATISFIED` | 默认不超额采购；允许超额时必须记录人工或策略授权 |
| `DELIVERY.FEASIBLE` | 客户有交付要求时比较交期；任一侧未知则转复核 |
| `REQUIREMENTS.ALL` | 对每个已注册 blocking requirement 逐条评估 |
| `SUPPLIER.ALLOWED` | 只有存在批准策略时执行；策略或状态未知则转复核 |
| `TOTAL.RECONCILES` | 行金额、已报价小计、币种和舍入规则必须可复算 |

候选排序只在 `eligible` 集合内进行。默认顺序为显式策略，不属于通用事实；必须保存排序配置版本和稳定决胜键。

## 5. 两个正交状态机

处理进度和商业处置必须分开，避免“流程结束”等于“应该出报价”。

### 5.1 处理状态 `process_status`

```text
received → parsed → validated → evaluated → awaiting_review → settled → artifact_ready
                                  └──────────────→ failed
```

### 5.2 商业处置 `disposition`

```text
undecided
├─ quote_draft
├─ partial_quote
├─ supplier_inquiry_required
├─ cannot_quote
├─ needs_customer_clarification
└─ cancelled
```

产物由 `disposition` 决定：

| disposition | 允许产物 |
|---|---|
| `quote_draft` | 报价草案 + 决策记录 |
| `partial_quote` | 明确标注未报价行的部分报价草案 |
| `supplier_inquiry_required` | 对外询价任务/清单，不生成报价单 |
| `cannot_quote` | 无法报价回复草案，不生成报价单 |
| `needs_customer_clarification` | 待客户补充问题清单 |
| `cancelled` | 审计记录，不生成商业文件 |

回答完 review item 只能表示该问题已处置，不能自动推导成 `quote_draft`。

## 6. 人工决策门

人工动作必须携带 `task_id`、`revision`、`review_item_id`、选择、操作者、时间和备注。服务端必须验证：

1. revision 仍是当前版本；
2. 选项确实属于该 review item；
3. 该项尚未回答；
4. 动作产生的 disposition 与允许产物一致；
5. 影响价格的动作会使旧产物失效并重新计算。

`request_supplier_quote`、`cannot_quote`、`accept_exception`、`change_requirement` 是不同商业动作，不能都压缩成“复核已完成”。

## 7. 知识与配置基线

项目需要知识层，但不是先做某个行业的向量库。应区分三类资产：

| 资产 | 示例 | 权威用途 |
|---|---|---|
| 业务事实数据 | 库存、成本、供应商报价、汇率 | 查询、匹配、计算 |
| 版本化规则目录 | MOQ、有效期、客户允许条件、审批门槛 | 确定性资格判断 |
| 非结构化知识 | 术语、SOP、政策说明、规则来源文档 | 检索、解释、辅助生成问题 |

规则目录至少包含：`rule_id`、版本、适用条件、输入字段、结果逻辑、严重度、来源、有效期和负责人。RAG 返回的文本只能用于解释或提出待确认事项；若要影响资格或价格，必须先转成经批准的结构化规则版本。

## 8. 行业扩展包

扩展包通过以下接口接入，不修改通用状态机和金额契约：

```text
schema_extensions     注册行业属性与数据类型
normalizers           原始字段到 requirement/attribute 的映射
rules                 行业资格规则
terminology           术语与展示名称
evidence_requirements 每类结论必须保留的证据
fixtures              行业正反例与黄金结果
```

例如航空扩展可以注册件状、证书和追溯；电子元器件扩展可以注册封装、温度等级和 RoHS；加工件扩展可以注册材料、尺寸公差和表面处理。通用核心不预设任何行业枚举。

## 9. 基准验收矩阵

在任何行业样例之前，至少用合成 fixture 验证：

1. 正常单币种报价可完整复算；
2. 缺单价不会生成 0 元报价；
3. 未知币种或缺汇率不会默认按 1 换算；
4. 无库存数量不会因“现货”而自动通过；
5. MOQ 超额在无授权时被拦截；
6. 重复料号累计分配不超过库存；
7. 单位不一致且无换算时进入复核；
8. 价格状态有效但日期过期时仍被拒绝，反之亦然；
9. 未注册行业要求不丢失并进入复核；
10. 人工接受例外后保留原要求、候选事实和授权记录；
11. stale revision 和重复回答被拒绝；
12. `supplier_inquiry_required` 与 `cannot_quote` 均不能生成报价单；
13. partial quote 只汇总已报价行，并显式列出未报价行；
14. 相同 RFQ、事实快照、规则版本和策略版本产生相同结果。

## 10. 当前实现与基准差距

当前实现已具备：多载体解析、精确 PN 查询、库存累计分配、状态/有效期/MOQ/件状过滤、Decimal 金额、revision、决策日志和草案输出。

进入 Baseline v1.0 验收前必须补齐：

1. 缺单价、非正价格和缺汇率必须转 `fail/unknown`，禁止回退为 0 或 1；
2. 引入逐规则四值结果，而不是只保存零散 rejection reasons；
3. 分离 `process_status` 与 `disposition`，修正“转人工/无法报价仍导出报价单”；
4. 建立通用 requirement registry 和未知要求的保留策略；
5. 增加 UOM、交期、数量未知、费用口径和未注册要求的基准测试；
6. 将航空相关字段放入首个行业扩展 fixture，用于证明扩展机制，而不是写进通用核心。

## 11. 完成定义

满足以下条件才可称为“RFQ 基准完成”：

- §9 的合成验收矩阵全部自动化通过；
- 任一报价数字都能回溯到事实快照、规则版本、策略版本和公式；
- 任一未知 blocking 条件都不会静默通过；
- 商业处置与产物类型严格一致；
- 新增行业只需扩展包，不需修改通用核心状态机和金额模型；
- 航空 A–F 案例作为第一套行业 fixture 通过，但不定义通用产品边界。
