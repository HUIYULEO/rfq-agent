# RFQ 业界流程对标与设计影响

> 日期：2026-11-01
> 目的：用公开资料核对"真实世界里 RFQ 怎么跑"，再把差异落成对《RFQ_SOLUTION_DESIGN》的具体改动建议。
> 方法：只采信可点击的公开来源（厂商文档、行业指南、法规原文），不与赛事素材混淆；赛事素材仍以《RFQ赛题解析》为唯一事实基线。
> 边界：本文不是法律或合规意见；出口管制部分只用于提示需要人工确认的边界。

## 0. 先纠正一个定位问题

我们做的不是"买方询价采购"，而是**供应商侧的报价（supplier-side quoting）**：收到客户 RFQ，决定报不报、报什么价、什么条件、多久交。《方案》§3.1 已经这么写了，但文中混用了买方视角的词汇（"候选供应商""供应商价格表"），容易被误读成 e-sourcing 平台。

业界对这两侧的流程划分很清楚：

| 视角 | 典型系统 | 核心动作 |
|---|---|---|
| 买方（sourcing） | e-sourcing、Ariba/Coupa、Findchips 类比价 | 发 RFQ 给 3–5 家、收报价、归一化比价、定标 |
| **供应商（quoting）** | CPQ、报价工作台、quote-to-order | 接 RFQ、查成本、定价、走审批、出报价、转订单 |

我们站在右侧。所以真正的对标对象是 **CPQ / quote-to-order**，而不是采购比价。这直接影响叙事：评委问"你和比价工具有什么区别"，答案是"我们不比价，我们出价"。

## 1. 业界的 RFQ 全流程（供应商侧）

综合 [Findchips 的 RFQ 指南](https://blog.findchips.com/rfq-process-electronic-components/)、[ChannelDock 的 quote-to-order 五步](https://channeldock.com/fr/blogs/b2b-quote-to-order-workflow/)、[Privé Aero 的航材 RFQ 流程](https://priveaero.com/faq)、[MG Aviation 的航材 RFQ 写法指南](https://mg-aviation.tech/articles/aircraft-parts-rfq-guide.html)，标准流程是 7 段：

```text
① 接收与识别   EML/Excel/PDF/正文；判 AOG 还是常规；判 RFQ / RFI / RFP
② 结构化       一行一料号：PN、可替代码、描述、数量+单位、可接受件状、证书、追溯、到货日、发货国
③ 可行性判断   有没有货、什么件状、能不能按期、要不要转外采
④ 定价         成本基线 + 加成/折扣；汇率、Incoterms、运费、关税
⑤ 例外审批     低于毛利底线、超信用额度、非常规交期、超授权折扣 → 路由给审批人
⑥ 出报价       价格、条件、证书、追溯、数量、交期、MOQ、发货国、有效期、条款
⑦ 转订单       客户接受 → 报价变成订单承诺（是否预留库存必须写明）
```

与我们现有设计（《方案》§6.2 的 13 步）对比：**主干一致，缺三样东西**——③ 的"货权/外采"判断、⑤ 的"审批矩阵"、⑦ 的"接受后的承诺边界"。

## 2. 一行 RFQ 该有什么（业界清单）

[MG Aviation](https://mg-aviation.tech/articles/aircraft-parts-rfq-guide.html) 给出的航材 RFQ 行字段，和我们数据模型的对照：

| 业界字段 | 我们的字段 | 差距 |
|---|---|---|
| Part number（含后缀/横杠） | `part_number` | ✅ |
| 可替代 P/N、PMA 是否可接受 | `requested_mpn` 有，但无"是否允许替代" | ⚠️ 缺 `allow_alternates` |
| Description | `description` | ✅ |
| 数量 + 计量单位（EA/FT/LB/KT/RL） | `quantity` + `unit_of_measure` | ✅（数据里出现 IN/FT，属异常） |
| **可接受件状（集合）**："NE, NS, OH" | `condition_required`（单值，等值匹配） | ❌ **语义错了**，见 §3.1 |
| 证书要求：8130-3 / EASA Form 1 / dual release / CoC | `certificate_required` | ✅ 有字段，但无规则 |
| 追溯要求：last operator / manufacturer / back-to-birth | 无 | ❌ 缺 |
| 到货日（need-by date，不是 ASAP） | `response_due_at` + `line_due_at` | ⚠️ 语义已区分，但数据里只有"截止报价日" |
| 发货国 | 供应商表有 `国家地区`，无"发货地" | ⚠️ 两个概念不同 |
| AOG 标志（必须独立、不能每行都挂） | `urgency` | ⚠️ 有字段、无 SLA |
| 剩余寿命 / 序列号 / 包装 | 无 | ❌ 缺（航材消耗件才需要） |

## 3. 五个必须改的设计点

### 3.1 件状条件不是"等于"，是"可接受集合"

**业界做法**：客户写"可接受的每一个件状"，例如"NE, NS, OH"（[MG Aviation](https://mg-aviation.tech/articles/aircraft-parts-rfq-guide.html)）；航材商的说法是"告诉我们你的维修方案最低接受什么件状，我们只报满足它的货"（[Privé Aero](https://priveaero.com/faq)）。常见代码：`FN`(factory new)、`NE`(new)、`NS`(new surplus)、`OH`(overhauled)、`SV`(serviceable)、`TST`(tested)、`INS`(inspected)、`RP`(repaired)、`AR`(as removed)。

**我们的做法**：`候选.件状 == 需求.件状` 等值匹配（《方案》§8.1、《计划》RULE-004）。

**影响**：客户要 `OH` 时，一个 `FN` 的候选本来更好更贵，但被我们判为"件状不符"直接拒掉——这在真实业务里是丢单的错。而客户要 `FN` 时，`NE` 应当被拒，所以**我们现有的黄金数据结论不变**（六个案例的件状要求都是单一 `FN`，FN 是最严的一档，等值与集合两种口径结果相同）。

**改法**：字段改成 `condition_accept: ["FN"]`（集合），规则为"候选件状 ∈ 可接受集合"；再增加一张**配置化的偏序表**描述"优于/等价"，**不得硬编码**行业默认排序（`OVR` 之外还有很多例外，如双释放证书会改变优劣）。缺集合时记 `unknown` → blocking。

### 3.2 "未声明"要当作"没有"，而不是"通过"

**业界做法**：比对报价时"not stated = no"——证书或追溯没写，就是没有（[MG Aviation](https://mg-aviation.tech/articles/aircraft-parts-rfq-guide.html)）。行业数据也说明这很常见：在其 2026 年 6–9 月收到的约 11.2 万条报价行中，**46.6% 没写证书、44.3% 没写追溯、9.6% 没写交期、65.6% 没写发货国**。

**我们的做法**：`unknown` 三态，且"缺失不等于不满足"（《计划》§3）。

**两者不矛盾，但表述必须收紧**：`unknown` 在我们的模型里是 `needs_review`（阻塞），等价于业界的"现在还不能用"。文档要写明：**unknown 不是 pass**，且对外输出时表述为"未提供，需供应商确认"，不要写成"不满足"。同时这条支撑一个很强的叙事：**市场上近一半的报价根本没有证书信息，所以"把人放在关键节点"不是技术不成熟，是业务本来就需要的动作。**

### 3.3 报价单要素不全，但不必改官方模板

**业界要求**：一份可比的报价至少要有条件、证书、追溯、可用数量、交期（天）、MOQ、发货国、价格有效期；整单还要有币种、Incoterms、付款条件、条款（[MG Aviation](https://mg-aviation.tech/articles/aircraft-parts-rfq-guide.html)、[Privé Aero](https://priveaero.com/faq)）。

**我们的现状**：官方模板只有 9 列（序号/产品名称/规格/型号/数量/单位/金额/销售总价/备注），无件状、证书、交期、MOQ、有效期（《赛题解析》§4.1 实测）。

**改法（不改官方模板）**：DOCX 仍用官方模板，另出一份**一页"报价依据与条件摘要"**（PDF 或附页），承载：每行件状/证书/追溯/可用数量/交期/MOQ/发货国/价格有效期，整单币种、Incoterms、报价有效期、库存不预留声明、未计入费用声明。模板"备注"列只写最关键的 1–2 条限制。这样既满足"结构等价于官方报价单"，又不丢业界必需信息。

### 3.4 时效不是"以后再说"，AOG 有明确 SLA

**业界做法**：常规 RFQ 同日回复；AOG 一小时内给出**有实质内容的**首答（不是回执）——[Privé Aero](https://priveaero.com/faq) 明确"qualified first response within one hour"。另一方面，B2B 制造业平均报价响应是 **24–72 小时**，而买方在发出询价后 **4 小时内**就形成倾向（[Go Autonomous 2026 基准](https://goautonomous.io/blogs/b2b-quote-response-time-benchmark-2026-how-long-manufacturers-take-to-quote/)），自动化报价可做到标准配置 60 分钟内。

**改法**：任务模型加 `urgency`（NORMAL/URGENT/CRITICAL/AOG）+ `sla_due_at`；AOG/CRITICAL 走独立的快车道与独立问题队列，不要混在 122 行的长表里（业界明确要求 AOG 单独发）。同时给演示一个硬指标：**B 案例端到端 ≤ 60 秒**（离线确定性路径），这比空谈"快"有力。

### 3.5 出口管制：必须有人确认，不能自动判定

**业界做法**：美国原产航材受 EAR 约束（[15 CFR 734.3](https://www.ecfr.gov/current/title-15/subtitle-B/chapter-VII/subchapter-C/part-734/section-734.3)），军品受 ITAR 约束；分类由出口方确定（EAR99 / 9A991 / 9A610 / USML）；还要过"三国家测试"（飞机所在地、注册地、控制人所在国，[15 CFR 744.7](https://www.ecfr.gov/current/title-15/subtitle-B/chapter-VII/subchapter-C/part-744/section-744.7)），并留存最终用户声明（[MG Aviation 出口管制指南](https://mg-aviation.tech/articles/aircraft-parts-export-control.html)）。该指南明确说"这些信息本就该写在 RFQ 里"。

**我们的现状**：只有 `国家地区` 字段，无任何合规检查。

**改法**：**不做自动判定**（我们没有分类数据，自动判 ECCN 等于编造）。做法是加一个**任务级合规确认项**：当出现跨境/美国原产线索时，生成一条 `scope=task` 的 review item——"本案涉及跨境供货，请确认最终用户与目的地已完成出口管制审查"，默认选项"已确认/转合规复核"，未确认则草案标红但**不阻断内部草案生成**。

## 4. 与既有决策的对照

| 已定决策 | 业界依据 | 结论 |
|---|---|---|
| 报价有效期 30 天 | "多数元件报价 30 天"（[Findchips](https://blog.findchips.com/rfq-process-electronic-components/)） | ✅ 保留，可从"纯假设"升级为"与惯例一致" |
| 不允许拆单/超额 MOQ | 业界确实把 MOQ 当硬约束，但会做"按 MOQ 报价"选项 | ⚠️ 不改默认，但 review 选项里要保留"按 MOQ 超额采购"（黄金数据已有） |
| 不允许模糊匹配 | 业界靠可替代 P/N 白名单，不是靠模糊 | ✅ 强化：加 `allow_alternates`，模糊匹配永远只做"提示候选" |
| 内部库存优先 | 业界是"有没有货/能不能按期"优先，不是"自有库存优先" | ⚠️ 保留为演示策略，但排序应可切到"交期优先" |
| 不实际扣库存 | 业界：不预留就必须在报价上写明 | ✅ 采纳，加"报价不预留库存"声明 |
| 人工确认边界 | quote-to-order 明确"例外在接受前路由给审批人" | ✅ 与我们的 gate 设计完全一致，可直接引用 |

## 5. 竞品与先例（说明我们不是拍脑袋）

- 商业侧：Go Autonomous 的 RFQ/Quote Execution 产品线，主张标准配置 60 分钟内出价（[基准报告](https://goautonomous.io/blogs/b2b-quote-response-time-benchmark-2026-how-long-manufacturers-take-to-quote/)）。
- 开源先例：`rfq-quote-agent` —— 把杂乱的 B2B RFQ（PDF/Excel/邮件）转成报价 PDF，**确定性定价 + 人工在环（Telegram）**（[GitHub](https://github.com/gourav-shokeen/rfq-quote-agent)）。它的架构选择与我们一致：模型负责理解，定价交给确定性代码，人负责放行。
- 行业标准：航空业有 ATA Spec 2000 Gen 2 的 RFQ/报价结构化报文（[Airlines for America](https://publications.airlines.org/products/spec-2000-gen-2-procurement-revision-2024-2)）。我们**不实现**它，但字段命名应可映射——说明文档里写一句"字段可映射至 Spec 2000 采购报文"即可体现专业度，切勿声称已实现。

## 6. 改动清单（按优先级）

| # | 改动 | 落在哪 | 成本 |
|---|---|---|---|
| 1 | `condition_required` → `condition_accept` 集合 + 可配置偏序 | 数据模型、RULE-004、黄金数据脚本 | 0.5h |
| 2 | 加 `allow_alternates`、`trace_required`、`ships_from` 字段 | 数据模型 | 0.2h |
| 3 | 报价"条件摘要页"（不改官方模板） | 输出层、方案 §7.5 | 0.5h |
| 4 | `urgency` + `sla_due_at`，AOG 独立队列 | 任务模型、状态机 | 0.3h |
| 5 | 任务级出口管制确认项（不自动判定） | review item 类型 | 0.2h |
| 6 | 报价声明：不预留库存、未计入费用 | 输出模板 | 0.1h |
| 7 | 文档：定位改为 supplier-side quoting，补 ATA Spec 2000 可映射说明 | 方案 §1/§2、说明文档 | 0.3h |

## 7. 调研没有回答的问题

- 国内航材分销的实际报价惯例（本次来源以欧美公开资料为主），可能与本设计有差异；
- 赛事数据里 `手工备件`、`产品范围` 等中文列的真实业务含义，公开资料没有对应解释；
- 各航司/维修单位对"件状最低要求"的实际写法分布——这决定偏序表该多复杂。这三项都需要业务方确认，不得由文档臆测。
