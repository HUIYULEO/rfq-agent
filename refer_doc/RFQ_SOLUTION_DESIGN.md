# MatrixNEXT RFQ 智能体方案设计

> 状态：Draft v0.2（审核后）  
> 日期：2026-11-01  
> 上游事实基线：[RFQ赛题解析.md](./RFQ赛题解析.md)  
> 配套需求清单：[RFQ_REQUIREMENTS_BACKLOG.md](./RFQ_REQUIREMENTS_BACKLOG.md)

> 审核修订与实施顺序：[RFQ_DEVELOPMENT_PLAN.md](./RFQ_DEVELOPMENT_PLAN.md)。实现以该计划第2、3节的五工具边界、时间、版本和字段级证据契约为准；下文为概念结构。

## 1. 文档定位

本文描述 RFQ 询价分析与报价智能体的产品方案和技术架构。赛事素材中的客观事实以《RFQ赛题解析》为准；本文只负责说明如何实现，不重复定义赛事事实。

## 2. 方案结论

项目应实现一个“证据驱动的 RFQ 报价工作台”，而不是普通聊天机器人：

```text
六个官方 RFQ 请求
→ 邮件与附件解析
→ 统一 RFQ 数据模型
→ 完整性和冲突检查
→ 内部库存/成本与供应商报价匹配
→ 确定性候选过滤和报价计算
→ 主智能体解释、追问和组织结果
→ 人工确认
→ 动态生成报价草案
```

Deep Agents 负责编排和异常处理；本地代码负责解析、校验、匹配及计算；MatrixOrigin Genesis 提供模型能力；MOI Workflow、Catalog 和 Agent 在验证稳定后承担平台展示和交互。

## 3. 产品目标与边界

### 3.1 目标用户

- 收到客户 RFQ 的销售人员；
- 报价专员；
- 需要核对供应来源和履约条件的供应链人员。

### 3.2 核心用户任务

用户提交一封 RFQ 邮件及其附件，系统完成：

1. 识别输入模式；
2. 提取 RFQ 基本信息和全部行项目；
3. 标记缺失、重复、冲突和异常；
4. 查询内部库存/成本和供应商价格；
5. 过滤不满足业务条件的候选；
6. 根据透明配置形成报价建议；
7. 展示数据来源、计算过程和风险；
8. 自动生成标明待复核的报价草案；用户确认后生成已复核版本。

### 3.3 非目标

- 自动收取真实邮箱邮件；
- 自动对外发送正式报价；
- 无人工确认的自主定价；
- 实时向外部供应商询价；
- 复杂审批、ERP 或 CRM 集成；
- 汽车模具或工业图纸报价；
- 为展示技术复杂度而构造多智能体系统。

## 4. 分阶段产品范围

### 4.1 V0：单案例垂直切片

使用第一组 Buyer C：英文邮件 + `Quote.pdf`。

目标是验证一条完整路径：

```text
解析邮件及 PDF
→ 得到 RFQ-0930 和 7 个行项目
→ 匹配价格依据
→ 形成报价建议
→ 生成报价草案
```

V0 是开发切片。六案例覆盖是本项目自行设定的 MVP 质量目标，不表示赛事明文要求六例全部通过。

### 4.2 V1：第一组开发集

覆盖第一组客户 A、B、C，验证三种输入模式：

- 正文表格；
- 邮件 + Excel；
- 英文邮件 + PDF。

### 4.3 MVP：六案例完整闭环

第一组作为开发集，第二组作为回归集。全部六个请求必须进入同一 RFQ Schema，并通过各自验收要求。

### 4.4 比赛交付版

在六案例闭环上增加：

- 可公开访问的智能体入口；
- 可视化处理状态；
- 报价草案下载；
- 平台能力说明；
- 三分钟演示视频；
- 公开说明文档和数据合规说明。

## 5. 总体架构

```text
┌──────────────────────────────────────┐
│ 交互与展示层                         │
│ MOI Agent 或公开 Web UI              │
│ 上传/选择样例、补充信息、确认、下载   │
└──────────────────┬───────────────────┘
                   ↓
┌──────────────────────────────────────┐
│ 应用服务层                           │
│ RFQ Task API、文件管理、状态和产物    │
└──────────────────┬───────────────────┘
                   ↓
┌──────────────────────────────────────┐
│ Deep Agents 编排层                   │
│ 计划、工具选择、失败恢复、解释和追问  │
└────────────┬─────────────────┬───────┘
             ↓                 ↓
┌─────────────────────┐  ┌─────────────────────┐
│ 本地确定性业务层     │  │ MatrixOrigin MOI    │
│ EML/PDF/XLSX 解析    │  │ Genesis 模型        │
│ Schema 与校验        │  │ Workflow            │
│ 数据匹配和报价规则   │  │ Catalog / Agent     │
│ DOCX/XLSX 产物生成   │  │ 平台运行记录        │
└────────────┬────────┘  └─────────────────────┘
             ↓
┌──────────────────────────────────────┐
│ 数据与证据层                         │
│ 原文件、标准 JSON、候选、决策、产物   │
└──────────────────────────────────────┘
```

## 6. 核心工作流

### 6.1 任务状态

```text
received
→ parsing
→ normalizing
→ validating
→ matching
→ pricing
→ needs_review / ready
→ generating
→ completed
```

失败状态：

```text
needs_input | partially_completed | failed
```

### 6.2 详细流程

1. **接收任务**：创建任务 ID，保存原始文件和文件摘要。
2. **邮件解析**：读取发件人、主题、日期、正文、附件名称和二进制附件。
3. **输入分类**：判断明细位于正文、Excel 或 PDF。
4. **附件解析**：保留原始行、表格位置、页码等来源信息。
5. **字段标准化**：映射为统一 RFQ Schema，保留原始值和规范值；重复行只标记，不自动合并或删除。
6. **质量检查**：检查缺失、重复、冲突、类型、日期和单位。
7. **候选匹配**：按 Part Number 查询内部物料和供应商价格。
8. **硬约束过滤**：有效期、MOQ、数量、件状、证书、交期和币种。
9. **候选排序**：应用明确配置，不允许模型自由决定权重。
10. **报价计算**：由确定性函数产生价格、金额和计算解释。
11. **智能体审阅**：整理结果、解释异常、生成需要人工回答的问题。
12. **人工确认**：确认低置信度字段、替代件、过期参考价及正式报价。
13. **产物生成**：生成动态行数的报价草案和完整决策记录。

## 7. 数据模型

### 7.1 RFQ 单据

```json
{
  "task_id": "",
  "source_email": "",
  "sender": "",
  "customer": "",
  "subject": "",
  "rfq_number": null,
  "inquiry_date": null,
  "response_due_at": null,
  "urgency": null,
  "currency": null,
  "delivery_destination": null,
  "global_requirements": [],
  "attachments": [],
  "items": [],
  "issues": []
}
```

### 7.2 RFQ 行项目

```json
{
  "line_number": null,
  "part_number": null,
  "description": null,
  "quantity": null,
  "unit_of_measure": null,
  "priority": null,
  "product_requirement": null,
  "requested_mpn": null,
  "condition_required": null,
  "manual_part_number": null,
  "product_scope": null,
  "certificate_required": null,
  "line_due_at": null,
  "source": {
    "file": "",
    "location": "",
    "raw_value": null
  }
}
```

### 7.3 报价候选

```json
{
  "part_number": "",
  "source_type": "internal | supplier",
  "source_id": "",
  "available_quantity": null,
  "cost_basis_type": null,
  "unit_cost_original": null,
  "currency": null,
  "exchange_rate": null,
  "unit_cost_cny": null,
  "moq": null,
  "lead_time": null,
  "condition": null,
  "certificate": null,
  "valid_until": null,
  "eligibility": "eligible | rejected | needs_review",
  "rule_results": [],
  "rejection_reasons": []
}
```

### 7.4 报价决策

```json
{
  "line_number": null,
  "part_number": "",
  "selected_candidate_id": null,
  "recommended_source": "internal | supplier | manual",
  "unit_cost_cny": null,
  "markup_rate": null,
  "recommended_unit_price": null,
  "quantity": null,
  "line_total": null,
  "confidence": "high | medium | low",
  "decision_reasons": [],
  "warnings": [],
  "requires_human_review": true
}
```

所有解析字段和报价决策必须保留来源。规范化值不能覆盖原始值。

## 8. 报价规则引擎

### 8.1 硬约束

候选出现以下情况时默认不可直接使用：

- Part Number 不匹配；
- 价格状态无效或已过期；
- 请求数量低于 MOQ 且不允许按 MOQ 采购；
- 可用库存不足且不允许拆单；
- 件状条件不满足；
- 必需证书不满足；
- 交期晚于客户要求；
- 币种或汇率缺失，无法得到统一成本。

### 8.2 候选排序

赛事未给出正式排序规则，第一版使用配置驱动策略。建议默认顺序：

1. 满足全部硬约束；
2. 数据仍在有效期；
3. 内部库存或供应商能够满足数量；
4. 统一币种成本更低；
5. 交期更短；
6. 稳定候选 ID 决胜，保证排序可重复。

内部库存是否优先、是否允许拆单以及成本口径均为待确认配置，不写死为赛事规则。

### 8.3 报价计算

第一版采用透明公式：

```text
报价单价 = 选定成本基线 × (1 + 加价率)
行金额 = 报价单价 × 报价数量
```

税费、运费、包装费、关税、汇率缓冲和价格取整分别配置。缺少配置时应显示“未计入”，而不是隐式假定为零。

### 8.4 人工确认边界

以下情况必须转人工：

- 没有合格候选；
- 只能使用过期价格；
- 多来源数据冲突；
- Part Number 模糊匹配；
- 件状或证书不完全一致；
- 需要拆单或替代料；
- 汇率、税费或额外费用缺失；
- 模型提取字段置信度不足；
- 正式对外报价。

## 9. Deep Agent 设计

### 9.1 为什么使用 Agent

输入载体、缺失信息和异常路径并不固定，需要根据当前状态选择解析、查询、补充信息或人工确认动作。Agent 的价值在于编排这些动作，而不是替代规则引擎。

### 9.2 第一版工具

| 工具 | 输入 | 输出 | 性质 |
|---|---|---|---|
| `inspect_input` | 文件列表 | 文件类型和处理建议 | 只读 |
| `parse_email` | EML 路径/内容 | 邮件元数据、正文、附件 | 只读 |
| `parse_spreadsheet` | XLSX | 表格、字段和来源 | 只读 |
| `parse_pdf` | PDF | 文本、表格、页码 | 只读 |
| `normalize_rfq` | 解析结果 | 统一 RFQ JSON | 确定性优先 |
| `validate_rfq` | RFQ JSON | 问题列表和严重度 | 确定性 |
| `find_internal_materials` | Part Number | 内部库存/成本候选 | 只读 |
| `find_supplier_quotes` | Part Number | 供应商候选 | 只读 |
| `evaluate_candidates` | RFQ 条件、候选、配置 | 合格/拒绝候选及理由 | 确定性 |
| `calculate_quote` | 选定候选、配置 | 单价、金额和计算轨迹 | 确定性 |
| `generate_quote_draft` | 已确认决策 | DOCX/XLSX/PDF 草案 | 写产物 |
| `request_human_review` | 问题及选项 | 待确认状态 | 人机协作 |

以上为内部函数清单。Agent仅暴露 `analyze_rfq`、`build_quote_options`、`apply_review`、`export_quote_draft`、`get_task_result` 五个业务工具，契约见开发计划。工具返回结构化对象，大量原始内容保存在任务文件中。

### 9.3 暂不使用子智能体

第一版不启用子智能体。只有满足以下证据之一才引入：

- 并行解析明显降低总耗时；
- 不同输入模式需要相互隔离的长上下文；
- 主智能体上下文被大量解析结果污染；
- 回归评测证明专用 Agent 提升准确率。

## 10. MatrixOrigin 集成

### 10.1 必选集成

- Genesis：作为模型服务；
- MOI Agent 或兼容的公开入口：作为最终展示候选。

### 10.2 条件集成

- Workflow：当附件输入、节点调试和 Agent 绑定稳定时使用；
- Catalog：当 Excel 导入和结构化查询稳定时承载数据表；
- 知识库：仅用于政策、术语和说明资料，不用于价格计算。

### 10.3 降级路径

已观察到 Excel 导入 HTTP 400。任何平台能力接入前必须通过独立验证，失败时保持：

```text
本地解析和报价服务
→ Genesis 模型调用
→ 自建公开 Web UI 或 MOI Agent 展示
```

核心业务测试不得依赖 MOI 页面操作。

## 11. 可靠性与可追溯性

每次任务至少保存：

- 原始文件摘要；
- 解析器版本；
- 标准 RFQ JSON；
- 字段来源；
- 原始候选数据；
- 每个候选的过滤理由；
- 使用的报价配置版本；
- 计算轨迹；
- 人工确认记录；
- 最终产物和生成时间。

相同的已校验 RFQ、业务数据快照、报价参考时间和规则版本，必须产生相同报价数字。模型重新提取可能波动，应独立评测提取稳定性；不要承诺从原文件起每次模型运行都完全相同。

## 12. 安全与数据合规

- 第一版只使用赛事提供的演示/脱敏数据；
- API Key、访问令牌和个人账号信息不得进入仓库、日志、截图或演示视频；
- 上传文件只用于当前任务，不用于未经授权的模型训练；
- 对外报价必须经人工确认；
- 解析失败时不将原始敏感内容直接回显到错误页面；
- 任务范围内保存中间结果和生成草案无需逐次确认；修改业务事实、确认正式报价或外部发送才需要确认。第一版不实现外部发送。

## 13. 验收策略

### 13.1 数据集划分

- 第一组 A/B/C：开发和调试；
- 第二组 D/E/F：固定回归验证。

### 13.2 核心指标

- RFQ 单据字段准确率；
- 行项目召回率和字段准确率；
- 来源追溯覆盖率；
- 异常识别召回率；
- 候选过滤规则正确率；
- 报价计算一致性；
- 六案例端到端成功率；
- 单案例处理时延和模型消耗。

### 13.3 最低验收

以《RFQ赛题解析》第 9 节六案例矩阵为最低事实标准，并增加：

- 所有报价数字有来源或公式；
- 不合格候选不会进入自动报价；
- 无可靠依据时转人工；
- 报价草案支持动态行数；
- 回归集未被写死或作为提示词示例泄漏。

## 14. 关键配置

```yaml
pricing:
  cost_basis_priority: TBD
  internal_inventory_priority: TBD
  default_markup_rate: TBD
  rounding_mode: TBD
  quote_validity_days: TBD
  allow_split_fulfillment: false
  allow_expired_price_reference: false

delivery:
  lead_time_tolerance_days: TBD

currency:
  base_currency: CNY
  missing_exchange_rate_action: human_review

charges:
  tax_policy: TBD
  shipping_policy: TBD
  packaging_policy: TBD
  tariff_policy: TBD
```

TBD 项必须在报价输出中显式暴露，未确定前不得伪装成正式业务规则。

## 15. 待决策事项

1. V0 首个报价草案采用官方模板的动态 DOCX，PDF/XLSX 后置；
2. 默认成本基线及内部库存优先级；
3. 加价率、取整和报价有效期；
4. 是否允许 MOQ 超额采购和拆单；
5. 税费、运费、包装费和关税的演示口径；
6. V0 使用规则解析、Genesis 提取或两者组合；
7. 最终入口使用 MOI Agent 还是独立 Web UI；
8. MOI Workflow 和 Catalog 的验证截止点。

## 16. 决策记录

| 日期 | 决策 | 原因 |
|---|---|---|
| 2026-11-01 | 以 `F:\Codex\MatrixNEXT` 为唯一正式项目资料目录 | 避免多份赛事文档发生分叉 |
| 2026-11-01 | 《RFQ赛题解析》作为事实基线 | 已完成赛事文件级核对 |
| 2026-11-01 | V0 单案例、MVP 六案例 | 区分开发切片和参赛完成度 |
| 2026-11-01 | 第一组开发、第二组回归 | 防止针对单个样例写死 |
| 2026-11-01 | 单主智能体 + 确定性业务工具 | 保持可测、可解释和低复杂度 |
| 2026-11-01 | MOI 集成保留降级路径 | 平台导入已出现 HTTP 400 |

