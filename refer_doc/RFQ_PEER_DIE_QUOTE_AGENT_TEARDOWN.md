# 拆解：汽车冲压模具 RFQ 报价 Agent（matrixorigin 参赛包）

> 拆解对象：`F:\Codex\MatrixNEXT\matrixorigin\matrixorigin`（23 个文件，2 份说明 + 3 个 skill）
> 拆解目的：提取可迁移到我方 `rfq-agent`（航空配件询价）的方法论，而不是抄它的行业常数。
> 日期：2026-11-03

---

## 0. 一句话定性

**它不是"聊天框 + 一堆 API"，而是"三本操作手册 + 三个确定性脚本 + 一份校准常数表"。**

编排者是一个通用 Agent（Claude / Codex 之类），它读 `SKILL.md` 决定下一步、调 `scripts/*.py` 拿结果、看渲染图做视觉判断、最后手写一份 `verification.json`。

- **LLM 不做算术**，只做三件事：路由（该走哪步）、看图（判形状/工艺）、写覆盖值（JSON）。
- **脚本不做判断**，只做确定性换算：几何 → 尺寸，尺寸 → 价格，价格 → Excel。
- **人保留否决权**：所有 `⚠` 行必须过目，视觉验证不可跳过。

这个分工是整套东西真正的价值所在——比任何一条公式都值钱。

---

## 1. 骨架：为什么是 3 个 skill

| Skill | 一句话职责 | 解决什么病 | 输入 → 输出 |
|---|---|---|---|
| `rfi-param-estimator` | 用 STEP 3D 反推开料尺寸 + 模具三围/重量 | **没有数**（RFQ 里字段空着或填 1） | RFQ 文件夹 → `estimation.json` + Gate0 Excel |
| `rfi-tooling-offer` | 视觉核验 + 成本模型定价 + 出报价单 | **数不对**（AI/DB 的尺寸、类型、模宽是错的） | Gate0 Excel + renders + PDF + PPT → OFFER Excel |
| `step-file-analyzer` | 纯 STEP 几何特征提取（包围盒/料厚/孔数/折弯数） | **看不懂 3D**（前两个 skill 的底层工具人） | `.stp` → JSON 报告 |

拆分的判据很干净：**按"数据缺哪一环"拆，不按"功能模块"拆**。
`step-file-analyzer` 不输出价格、不输出模具尺寸，只输出零件几何——它被上面两个 skill 复用，是共用底座。

### 1.1 每个 skill 都是四层结构

```
skills/rfi-tooling-offer/
├── SKILL.md                        ← ① 决策层：frontmatter=路由器，正文=SOP
├── references/pricing_calibration.md   ← ② 知识层：公式 / 陷阱 / 反面教材 / 校准实例
├── scripts/generate_offer.py       ← ③ 执行层：纯确定性，无 LLM
└── assets/verification_template.json   ← ④ 契约层：人机接口的 schema
```

四层各自的写法很讲究，值得逐条抄：

**① 决策层 `SKILL.md`**
- frontmatter 的 `description` 不是简介，是**路由器**：一句话塞进触发词（「收到RFI询价帮我报价」「Gate0报价验证」「模具报价 运费」）+ 完整流程 + 输入清单。Agent 靠它决定"这个任务该不该装载我"。
- 正文是 SOP：输入 → 分步动作 → 每步的产物 → 汇报格式。
- 它明确写了**什么时候不该用**（"已知边界：模具尺寸公式只覆盖 Progressive / Transfer；其他类型提示用户，不要硬算"）。这句话比多写十个功能都重要。

**② 知识层 `references/*.md`**
- 装三样东西：**公式**（带校准样本）、**陷阱清单**（表格式，陷阱/误检/真相/动作四列）、**反面教材**（"禁止复用：Transfer 禁止用 pitch×工位数算长度——BU 伪公式，实测偏短 50%+"）。
- 反面教材是这套件里最贵的资产。它把"我们踩过什么坑"固化成可执行的禁令，而不是散落在聊天记录里。
- 文档开头写"计算前必读"，把阅读顺序也管起来。

**③ 执行层 `scripts/*.py`**
- 自包含、可 CLI 单独跑、只依赖 openpyxl（`generate_offer.py` 连 pandas 都不要）。
- 脚本会**主动校验并打印告警**（工步数不符、模长 < pitch×步数），但不擅自改数——改数是人的事。

**④ 契约层 `assets/verification_template.json`**
- 一个带 `_字段说明` 的样例 JSON，每个字段写明"必填/可选/默认值从哪来"。
- 这是 LLM 写覆盖值的唯一格式依据，也是脚本读 JSON 的唯一依据。**双方靠这个文件对齐，不靠文档描述。**

---

## 2. 工作流怎么搭的

### 2.1 主链 5 步（加一步可选）

```
RFQ 包（邮件/HTML/Excel/PDF/PPT/STEP/渲染图）
   │
   ├─[0] LLM 抽邮件意图（本仓库未实现，建议用 MOI Genesis）→ 项目号/BU/目的地/零件清单
   │
   ├─[1] rfi-param-estimator
   │      estimate_params.py  → estimation.json（每字段带 source）
   │      run_prediction.py   → Gate0 Excel（v7 TabPFN 预测价）
   │
   ├─[2] rfi-tooling-offer 第 1~3 步（人机混合，最重要）
   │      读 Excel/PPT/PDF/renders → 逐 line 交叉验证 → 手写 verification.json
   │
   ├─[3] generate_offer.py
   │      verification.json + Gate0 Excel → OFFER.xlsx（3 sheet）
   │
   └─[4]（可选）analyze_supplier_offer.py
          供应商真实报价 → benchmark JSON + md 汇总表
```

### 2.2 交接物是"文件"，不是"函数调用"

两个 skill 之间唯一的耦合是 **Gate0 格式 Excel**。
`rfi-param-estimator` 的 SKILL.md 末尾专门写了「接力 rfi-tooling-offer：把 Gate0 Excel + renders 交给它，从其第 1 步开始正常走」。

好处：任一步可以人中途接手、可以重跑、可以换人做。坏处：没有编排器，跑全链靠 Agent 自觉——**不可一键复现**。这点在第 9 节会专门讲。

### 2.3 三处人工卡点（设计出来的，不是没做完）

| 卡点 | 位置 | 机制 |
|---|---|---|
| 参数估算确认 | `estimation.json` 落盘后 | SKILL.md 明写"**主动把带 ⚠ 的行给用户过目，用户可手改 JSON 后再继续**" |
| 视觉工艺验证 | 第 2 步 | "**核心，不可跳过**"；并行派子代理（每组约 7 个零件）加速 |
| BU 指定优先 | 第 2 步末尾 | "若 BU RFI/HTML 明确指定 Die Material Structure=Steel，**以 BU 指定为准**（报价须响应询价），仅在备注提示铸铁降本选项" |

第三点是对"技术最优"的主动让步——报价是商业行为，不是工程最优解。这条在航空件场景同样成立（客户要 EASA Form 1，你不能拿"PMA 更便宜"去说服他）。

---

## 3. 数据流：文件即接口

| 产物 | 生产者 | 消费者 | 关键内容 | 设计要点 |
|---|---|---|---|---|
| `estimation.json` | `estimate_params.py` | 人 → `run_prediction.py` | 每 line 14 个字段，**每字段 `{value, source}`** | 可手改；`warnings[]` 逐行附带 |
| Gate0 Excel | `run_prediction.py` | 第 2 步 | 预测价 + features + 几何诊断 | 复用业务方模板（`Gate0业务报价模板.xlsx`），不另造格式 |
| `verification.json` | **LLM/工程师手写** | `generate_offer.py` | 覆盖值 + `steps_desc` + `remark` | 与 `assets/verification_template.json` 对齐 |
| OFFER.xlsx | `generate_offer.py` | 客户/BU | 3 个 sheet | 单文件自解释，含"视觉验证说明" |
| benchmark JSON | `analyze_supplier_offer.py` | 人 → 定价校准 | 供应商逐行 + 吨价分档 + 费用结构 | 只增不改，文件名含日期 |
| `supplier_benchmarks.md` | 同上（追加写） | 人 | 一行一样本汇总表 | 人类可读的索引 |

### 3.1 `source` 标记是数据流的骨架

`estimation.json` 里每个字段长这样：

```json
"tool_length_mm": { "value": 4850, "source": "computed" },
"tool_height_mm": { "value": 1060, "source": "html" },
"steps_qty":      { "value": 10,   "source": "computed" }
```

`source` 只有四个值，语义严格：

| source | 含义 | 下游怎么对待 |
|---|---|---|
| `html` | 直接采信 RFQ 原值 | 保留 |
| `computed` | 公式/几何算出 | 报告时注明"这些行是算出来的" |
| `press` | 来自压机库（装模高度） | 与 HTML 交叉验证过 |
| `default` | 兜底表，**必有 ⚠** | 必须人工确认 |

这和我方 `rfq_agent` 每行的 `evidence: {file, container, row}` 是同一思路的不同侧面：**一个标"值从哪来"，一个标"值在原文件哪个格子"**。两者叠起来才是完整的可追溯。

### 3.2 `warnings` 贯穿全链，且不静默

同一行数据在三个阶段都会被挑刺：
1. 估算阶段 → 写进 `estimation.json` 的 `warnings[]`；
2. 出单阶段 → `generate_offer.py` 边算边 `warnings.append(...)`；
3. 落盘阶段 → 写进 Excel 第 3 个 sheet「⚠ 校验告警」+ 控制台打印。

例：`"Line 9: Progressive 模长 860mm < pitch×步数 215×10=2150mm，物理不可能（级进模模长≈pitch×步数），请修正模架或一出几"`（`generate_offer.py:255`）

注意告警文案的写法：**不只说错，还说为什么错、应该怎么改**。这是给工程师看的，不是给日志看的。

---

## 4. 中间参考文档怎么处理

五类载体，五套提取法，一套优先级。

### 4.1 载体 → 提取方式

| 载体 | 提取手法 | 用途 | 已知坑（文档里写死的） |
|---|---|---|---|
| **renders 渲染图** | 8 视角 PNG（front/back/left/right/top/bottom/iso/iso2/**montage**），先看 montage | 判真实形状、折弯数、是否深拉伸、tool type | 曲面细分产生**假圆柱面**→假折弯；大曲面件 bend_count 8~11 实为 2~5 道 |
| **RFQ 工艺卡 PPT** | python-pptx 逐 slide 抽文本（含表格）→ **存 txt 一次读完** | **最权威**的工序序列、压机吨位、料宽算式、一出几 | `Slide N = Line N`；工序数与 Excel P 列差 1 时**以 P 列为准**、内容按 PPT 压缩/补 ID |
| **2D 图纸 PDF** | PyMuPDF ① `page.get_text()` 抽标题栏文字 ② `page.get_pixmap(matrix=Matrix(2.5,2.5))` 渲染位图给视觉模型 | 材料牌号/料厚/孔数/公差/改版记录 | ① PDF 挂错/错版（标题栏是别的零件）→ 不采用 + warning ② 料厚与 RFI 矛盾 → 按 RFI/PPT 报 + warning ③ 一出二两个 PDF 哈希相同（缺一件图）→ warning ④ 无 PDF → 只靠渲染图 + PPT + warning |
| **STEP 3D** | cadquery：AABB、料厚、孔数、折弯数、成型面数、体积 | 反推开料尺寸、模具三围 | 文件夹里混进**压机 STEP**（体积大 1~2 个数量级）→ 按名字子串匹配，否则按体积量级排除 |
| **FullStep HTML** | pandas.read_html，取列数 ≥60 的那张表 | 原始参数 + PRESS ID + Plant | 模具 L/W/重量是**占位值/伪公式**，一律重算 |

关键细节：**PDF 要读两遍**——先抽文字（快、准、拿标题栏数字），再把页面渲染成位图交给视觉模型（看形状、看孔位）。纯文本会丢掉图形信息，纯看图会看不清小字。这个"双读"是实战出来的。

### 4.2 优先级阶梯（矛盾时谁赢）

```
PPT 工艺卡 ≈ 2D 图纸  >  RFI Excel  >  STP 特征 / AI 预测
```

写在一处，然后**逐条贯彻**：
- 开料参数：步距/料宽/料厚异常 → 以 PPT 的 `MATERIAL DIM.（1,2x230xcoil p:182）` 覆盖 RFI 的 H/I/J 列 + warning；
- 材料牌号：PDF 1.2mm vs RFI 1.6mm 矛盾 → 按 RFI/PPT 报 + warning；
- 模具类型：HTML 的 CODE 前缀推断会错 → PPT Slide N 交叉验证；
- 一出几：Excel N 列与 PPT 矛盾 → **用料宽算式物理判定**（`6+396+25+396+6=829` → 2 件并排），改 pps + warning。

注意最后一条的方法论：**不投票、不取平均，而是找一个物理等式一票定死**。航空件同理——件号重复、单位混用时，正确的做法不是猜，是找那个能对上账的等式（数量 × 单价 = 金额）。

### 4.3 外部知识库：压机库

`press_db.py` 是一个很好的"外部表格当知识库"的样板：

- 输入：`PressInfo/<工厂>/Database for *press*.xlsx`，列 A=压机号、B=吨位、C=装模高度（可能有多行 `430mm\ntransfer`）、I=模重上限、J/K=最大模长宽；
- 归一：`V202 - 250T` → `V202`（BU 的 HTML 爱在压机号后面接吨位）；Plant 全名 → 文件夹名（`SSMATT Sodecia ... Attendorn` → `Attendorn`），带别名表；
- **缓存**：解析一次写 `press_db_cache.json`，之后直接读 JSON；
- **表头不写死**：找"第 2~6 行里 A 列含 press 的那行"当表头；
- 兜底：解析不出来返回 `{}`，调用方 warn 后用默认表，**从不假装有数据**。

这套"松散结构 Excel → 归一 → 缓存 → 缺失即返回空"的模式，可以直接套到我方价格资源表 / 内部物料成本表上。

---

## 5. 定价逻辑怎么处理

### 5.1 三层漏斗，不是查表

```
L1 参数量   重量(kg) / 模具三围(mm)         ← 决定 80% 的价格
L2 吨均价   AC = 重量(t) × 吨价(¥/t) × 调整系数   ← 分类型/分材质/分重量档
L3 费用     BA = AC + CF + 样件 + 运费 + HLTO + 夹爪 + 设备
```

报价的准确率主要不在 L2 的吨价准不准（供应商之间天然差 15%），而在 **L1 的重量和模架尺寸准不准**。文档里写得很直白：

> 用供应商重量 + 分档吨价后，我方 AC 中位差仅 **+3.6%** —— 基准吨价量级正确，主要改进点是**重量估算**和**费用参数**。

这句话是我看过的最诚实的一句自我评价，也是整个定价设计的指导原则：**先把几何量算对，再谈价格模型**。

### 5.2 AC 六分项（成本结构）

```
AC = 设计 W(8%) + 机加 X(25%) + 组装调模 Y(25%)
   + 材料 Z(22%) + 热处理表面 AA(5%) + 标准件 AB(15% = 残差)
```

比例来自 one-shot 实例回归（XINJIN/CPM/KPM/SEA-TOOL，波动 ±3% 内）。
代码实现注意两个细节（`generate_offer.py:54`）：
- 前五项各自 `round(ac*share/100)*100` 取整到百；
- 第六项 **`AB = ac - 前五项`**，用残差兜住取整误差，保证 `SUM(W:AB) == AC` 恒等。
  写 Excel 时 `AC` 是公式 `=SUM(W:AB)`，不是常量——**报表自校验**。

### 5.3 吨均价 + 调整系数 + 最低收费

| 类型 / 材质 | 吨价 ¥/t | 最低收费 ¥ |
|---|---|---|
| Progressive 钢 | 45,000 | 85,000 |
| Progressive 铸铁 | 42,000 | 85,000 |
| Transfer 铸铁 | 35,000 | 150,000 |
| Transfer 钢 | 40,000 | 150,000 |
| Tandem | 40,000 / 42,000 | 150,000 |

调整系数**连乘**：
- 料厚 > 3mm（厚板冲裁，冲头需特殊材质）：×1.35
- 高强钢（yield ≥ 340，正则匹配 `(CR|HR|HX|DP|CP)\s*3[4-9]\d`）：×1.05
- 复杂多面体/大压型/CAM 侧冲：×1.10~1.25（人工传）

```python
def auto_price(tool_type, tool_material, weight_kg, thick_mm, material):
    rate = TON_RATE.get((tool_type, tool_material), 42000)
    mult = (1.35 if thick_mm > 3 else 1.0) * hs_factor(material)
    return max(MIN_CHARGE[tool_type],
               round(weight_kg/1000 * rate * mult / 1000) * 1000)
```

**最低收费是显式的地板，不是线性折算。** 文档解释了为什么：小模（<1t）供应商不按吨位线性报价，而是"一刀切 10 万/行"；v7 模型输出的是市场吨价口径，不含这个行为，所以地板价交给工程师阶段兜底。

### 5.4 其他费用（每一项都有样本来源）

| 项 | 规则 | 样本依据 |
|---|---|---|
| CF 检具 | `max(Prog 6k / Transfer 15k, 5%×AC)`，上限 Prog 25k / Transfer 60k | CPM 4.9%、SEA-TOOL 4.2% |
| HLTO 母线调试 | 2 人 × 7 天 = 40,000/套 | 供应商按模重 20k~45k 浮动 |
| 样件 | Buyoff 150 件；Prog 3,000 / Transfer 10,000 加工费 | ⚠ 供应商实为 **300 件 + 加工 + 运输**，合计 ≈4~5%×AC，可用 `sample_fee` 覆盖 |
| 夹爪（仅 Transfer） | 数量 × 7,500 + 30,000 安装 | CPM 7.5k/个、SEA-TOOL 4k/个（取高者） |
| 海运费 | `FreightCalculator`，四段拆分 | 按目的地/重量/尺寸/箱型 |
| 交期 | Prog 15/20/22/24/32 周；Transfer 18/24/26/30/38 | 供应商实测更短（CPM 15/18/20/22/30） |

注意"夹爪数量"的来源标了 `DB值，经常偏大，可人工修正`——**默认值来自数据库，但明确告诉你它可能错，并给了覆盖入口**。这个模式要抄。

### 5.5 内部公式链

```
BA 总价 = AC + CF + 样件 + 运费 + HLTO + 夹爪 + 设备
BB 外币 = BA / 8.1135
BR SGVT销售价 = BB × 2.3809    ← one-shot 内部系数，注释写"勿改"
AD 吨均价 = AC / (重量/1000)    ← 仅供内部核算
```

汇率和 BR 系数都是**命名常量 + 注释说明来历**，不散落在公式里。

### 5.6 运费：不是拍一个数，而是"装箱优化"

`freight_calculator.py` 是整套里唯一"自带优化器"的模块：

- 20+ BU 目的地 → 港口 / 国家 / 内陆公里 / 派送费 / 区域系数；
- 自动判箱型：LCL（<5t 且 <4cbm）/ 20OT / 40OT / 40FR，带尺寸适配检查；
- 费用四段拆：国内提货 + 港口操作 + 海运（base + per_kg 或 per_cbm）+ 目的港费 + 派送；
- **`batch_consolidated_cost()`**：同 BU 同批次多套模具，按重量装进 20OT 整箱、费用按重量分摊——单个 LCL 报价累加会贵 30~40%；
- 超限件单独整箱，不硬塞。

这段代码的启示：**费用项里最容易出错、也最容易做出差异化的是"物流/分摊"，而不是单价本身。** 单个报价累加 vs 整批合并，差 30%。

### 5.7 工步描述：为什么两套语义不能合并

这是全篇最"工程"的一段，也是最能体现"懂行"的地方。

| | Progressive 级进模 | Transfer 传递模 |
|---|---|---|
| 工步数含义 | 条料前进的站数（每站走一个 pitch） | 压力机的操作数（OP10~OPn） |
| 空步 ID | 有，占条料位置 | 一般无，每个 OP 都是实活 |
| 与模长关系 | ✅ 模长 ≈ pitch × 步数 + 余量 | ❌ 无 pitch 关系；模长 = f(轮廓, 工位排布, 夹爪空间) |
| 描述格式 | `1.BL+PI→2.TR→…→N.CPI` | `OP10 BL+PI→OP20 TR→…→OPn SEP+CPI` |

配套的三条校验规则（写进脚本，不是写进文档就完事）：
- Progressive：`tool_L ≥ pitch × steps × 0.95`，违反 = **物理不可能**，必须改；
- Transfer：**禁止**用 `pitch×步数` 或 `工位数×常数` 估模长，只做下限检查 `tool_L ≥ 零件传递方向尺寸`；
- 通用：`count_stations(steps_desc)` 必须 == BU 要求的 `steps_qty`，不符告警。

反面教材记录得很具体：`Transfer 禁止用 pitch × 工位数 + 余量 算长度（BU 的伪公式）。6334 #2 验证：BU 公式算 1740mm，实际供应商 3700~3840mm，偏短 50%+，方向性错误。`

**这类"某个维度上公式根本不存在"的结论，比任何公式都重要。** 它省下的是几十万级的报价错误。

---

## 6. 信任矩阵：整套东西的核心资产

`pricing_calibration.md` §4.5 有一张表，把每个字段的**管线来源、可信度、该做什么**列清楚：

| 字段 | 来源 | 可信度 | 动作 |
|---|---|---|---|
| 模具类型 | HTML CODE 前缀（TPA→Prog / TTA→Transfer） | ⚠ 会错 | PPT Slide N 交叉验证 |
| 一出几 | HTML "PARTS PER STROKE"，缺省=1 | ⚠ 常错 | 料宽算式物理验证 |
| 步距 | HTML Blank Pitch 直填，无校验 | ⚠ 可能错 | PPT p: 值 + MUD 反推 |
| 工步数 | HTML "TOTAL NUMBER OF STAGES" | ✅ 较可靠（BU 指定） | 保留，station 数必须 = P |
| 模架尺寸 | HTML 字段或占位值 | ⚠ Transfer 大件常偏大 | Prog 宽 ≈ 料宽+400~500 校验 |
| 模具重量 | `L×W×H×7.85×0.6` 公式 | ⚠ 系统性偏高 | 按填充率 0.45 重估 |
| 材料牌号 | v5 物理解码 | ✅ 较可靠 | PDF 矛盾时 warning |
| AI 的 AC | XGBoost（MAPE 11%） | ⚠ **>5t 可信；<2t 高估 1.2~1.8×** | 仅对比不照抄 |
| AI 的运费/HLTO | 硬编码（运费一律 30k） | ❌ 不可用 | 用真实算法 |

三件事值得学：
1. **它是"负样本知识"**——每一条都是从真实报价对不上账反推出来的，不是设计出来的；
2. **它给了动作，不只是判断**——"⚠ 会错"后面永远跟着"怎么改"；
3. **它有量化边界**——"<2t 高估 1.2~1.8×""Transfer 大模比值 0.60~0.80"，可检验、可推翻、可迭代。

还有一个反直觉的细节：文档里区分了"检测器口径 vs 人工口径"：

> 检测器输出的是**几何折弯区数量**，不是人工数的"主折弯工序"数。弧形面板族 pred ≈ 2~3× 人工值属**正常**，不是误检。报价时工步以人工验证为准，别照抄 bend_count。

**"模型没坏，是你用错了口径"**——能写出这句话，说明他们真的核对过 150 个真值。

---

## 7. 校验三层 + 回归飞轮

### 7.1 三层校验

| 层 | 手段 | 例子 |
|---|---|---|
| 物理自洽 | 数量级/几何关系 | 模长 ≥ pitch×步数；模宽 ≥ 料宽；pitch ≥ 零件投影 |
| 交叉验证 | 多载体比对 | PPT vs Excel vs PDF vs 渲染图 |
| 历史回归 | 与真实报价比 | 94 套盲测，中位 APE 11~13% |

第三层的结论很有说服力：
> 供应商两家之间同行价差中位 **14.9%**（P90 65%）——AI 中位误差已**小于**供应商天然散布。

**给自己定的标准不是"准"，而是"比人的分歧更小"。** 这是个能拿去答辩的指标定义。

### 7.2 飞轮是"半闭环"，且它自己知道

`analyze_supplier_offer.py` 干四件事：解析供应商报价 → 打印汇总/吨价分档/费用结构 → 与我方核验逐行对比（AC 差 >30% 打 ⚠）→ 样本入库（JSON + md 追加一行）。

但**它不改任何常数**。规则写得很明确：

> 样本 <3 家前不修改第 2 节基准价，只在此记录观察值；≥3 家后取中位数更新基准。

于是留下一个已知的敞口：文档里写着"Transfer 吨价 35k **偏低，待 S3 确认后上调至 ~42k**"，但 `generate_offer.py:24` 里还是 `35000`。
飞轮转了，但没接到引擎上——**这一步是人工的**。这是我方要改进的点（见 §9）。

---

## 8. 产出物清单

| 产出 | 形态 | 给谁看 | 特点 |
|---|---|---|---|
| `estimation.json` | JSON | 工程师 | 可手改；每字段带 source；逐行 warnings |
| Gate0 Excel | xlsx | 下游 skill | 复用业务方模板，不另造格式 |
| `verification.json` | JSON | 程序 | 人机接口契约；`_字段说明` 自解释 |
| **OFFER.xlsx** | 3-sheet xlsx | 客户 / BU | 见下 |
| benchmark JSON + md | JSON + md | 定价校准 | 只增不改，文件名带日期 |
| 汇报表 | 控制台/对话 | 用户 | 逐行 AC/BA、与 AI 差异 %、⚠ 行、合计 |

**OFFER.xlsx 的三个 sheet，各有不可替代的作用：**

1. **`MCCF OFFER`** — 64 列的业务报价单，逐行：产品图路径、图号、模具类型/材质、开料参数、一出几、工步数、**工步描述**、模具尺寸重量、六分项拆解、CF、条款、样件、运费、HLTO、夹爪、总价（本币/外币）、交期、材质、SGVT 售价。
   - 金额列全部是**公式**（`AC=SUM(W:AB)`、`AD=AC/(V/1000)`、`BA=AC+AE+AO+AP+AS+AW+AZ`、`BB=BA/汇率`、`BR=BB×2.3809`）→ 客户改任一项，全套自动重算。
2. **`AI报价对比`** — 逐行 AI 的 AC/BA vs 人工核验 AC/BA + 差异 % + 箱型 + 海运费 + **AI 判断问题（视觉验证）**。
   - 这一页是"我们不是把 AI 的数字抄一遍"的证明，也是答辩时最有说服力的一页。
3. **`视觉验证说明`** — 报价人、单位、默认公差、成本结构、运费口径、HLTO/样件/夹爪口径、交期口径、内部公式、备注、**⚠ 校验告警清单**、逐行核验备注。
   - 把"每一条假设"写在报价单里，而不是藏在心里。这是可解释定价的落地方式。

---

## 9. 哪些能搬到我方（航空配件 RFQ）

结构是**同构**的，知识层完全不同：

| 冲压模具 | 航空配件 | 同构点 |
|---|---|---|
| 模具重量 → 吨价 → AC | 件成本 → 加价率 → 单价 | 都是"物理量/成本量 × 单价 × 系数" |
| 开料尺寸缺失 → STEP 反推 | 参考表无价 → 需估算/询价 | 都是"没有数怎么办" |
| PPT/PDF/RFI 优先级阶梯 | 邮件正文/附件/PDF 表单优先级 | 都是多载体冲突裁决 |
| 信任矩阵（字段级） | 价格表/成本表/客户要求可信度 | 都是"哪个数能信" |
| 工步描述语义（Prog vs Transfer） | 件状/证书语义（全新/翻修，EASA vs PMA） | 都是"两个词看着像但不可混算" |
| benchmark 回归 | 成交价回流 | 都是数据飞轮 |

### 9.1 建议直接搬的 6 条

1. **字段级信任矩阵**。我方现在有 `evidence`（值在哪），但没有 `trust`（值能不能信）。建议在 `pricing.py` 的 candidate 上再加一维：每个数据源标注可信度 + 已知偏差方向（例："内部成本表的标准成本长期低于市场价 ×0.8"、"价格资源表 PDF 导入的单价小数位易错"）。有了这一维，"取最低价"才敢用。

2. **来源优先级阶梯，写在一处，逐条贯彻**。我方已有 `terms.py` 核对客户要求，但没有"两个载体冲突时谁赢"的总规则。建议在 `RFQ_SOLUTION_DESIGN.md` 里定死：**邮件正文明确条款 > 附件表格 > PDF 表单 > 模型推断**，并在 `ingest.py` 冲突处统一引用。

3. **中间产物 JSON + `source` 标记 + `warnings[]` + 可手改**。我方 `draft-r<rev>.json` 已经很接近，缺的是**每字段的 source**。补上之后，"这个价从哪来"能一路追到"这个数是算的还是抄的"。

4. **`_字段说明` 式的自解释契约**。我方的 review item / line schema 可以照 `verification_template.json` 的写法，在每个 JSON 模板里带一段 `_字段说明`，把"必填/可选/默认值从哪来"写进文件本身，而不是只写在 README。

5. **反例库单独成一个文件**。我方现在把坑散在 `RFQ_REQUIREMENTS_BACKLOG.md` 和各 review 里。建议建 `refer_doc/RFQ_TRAPS.md`，用四列表（陷阱/误判/真相/动作）维护，每条都带真实案例编号（如"案例 C：PDF 表单 4 条响应要求被当正文条款"）。**这是最值钱的资产，会一直涨。**

6. **"比人的分歧更小"作为准确率标准**。我方可以做同样的事：拿 6 个案例 + 人工核价结果，算我方与人工的中位偏差，再对比"两个采购员之间的偏差"。答辩时这个指标比"准确率 95%"可信得多。

### 9.2 建议不要搬的 3 条

1. **外部绝对路径**。`D:\Projects\RFI_db\...`、`D:\Projects\Playground\PedroDB\PressInfo`、`D:\Downloads\...` 写死在脚本顶层（`estimate_params.py:26`、`press_db.py:29`、`run_prediction.py:25`）。换机即死。→ 用配置/环境变量，缺失时**降级并明说**（这点 `press_db.py` 做对了：返回 `{}` + warn）。
2. **"飞轮只记录不回灌"**。`analyze_supplier_offer.py` 只写样本，常数靠人手改，于是出现 35k→42k 的悬空。→ 我方应让标定参数**从样本文件读**（`calibration.json`），脚本只负责追加样本 + 重算中位数，人只做"批准"这一步。
3. **"全靠 Agent 自觉串流程"**。三个 skill 之间没有编排器，谁先谁后靠 SKILL.md 里的一句话和人的记性。→ 我方 `service.py` 已经是显式状态机，这点做对了。**不要把状态机换成提示词。**

### 9.3 一个具体建议：把"定价"拆成两个文件

现在我方 `pricing.py` 里混了三样东西：候选构造（查表/匹配）、硬约束过滤、加价计算。冲压那套的做法值得借鉴——**拆成"引擎"和"常数"**：

```
pricing_engine.py   ← 逻辑：候选、约束、加价、复核项（我方已有，保留）
pricing_params.json ← 常数：加价率、分档、各类费用、可信度权重、标定日期
```

好处：常数可以被人 review、被 `analyze_supplier_offer` 类脚本自动更新、被 git diff 看见"这次报价策略改了什么"。现在这些数字埋在 `policy.py` 的 `DEFAULT_POLICY` 里，改一行没人注意。

---

## 10. 复现风险（本机实测）

| 依赖 | 状态 | 影响 |
|---|---|---|
| `pipeline_core`（`D:\Projects\RFI_db\06_ToolPrice_Web\backend`） | ❌ 不存在 | `estimate_params.py` / `run_prediction.py` 直接 import 失败 |
| `PressInfo`（`D:\Projects\Playground\PedroDB\PressInfo`） | ❌ 不存在 | 模高只能走默认表（且有 ⚠） |
| `cadquery` | ❌ 未安装 | STEP 几何全不可用 |
| `openpyxl` 3.1.5 / `pandas` 3.0.0 | ✅ 有 | **`generate_offer.py` + `freight_calculator.py` 这条纯规则路径可跑** |

结论：**这套东西能带走的部分是"方法论 + 规则定价 + 运费优化 + Excel 生成"，带不走的是"几何/ML 预测那半截"。** 队友 README 里也主动承认了这一点：
> 如果队长环境不齐，可以只走 `rfi-tooling-offer` 的规则报价路径，不影响演示核心流程。

### 代码层面的三个小问题（如果要复用需先修）

1. **HLTO 硬编码不一致**（`generate_offer.py:244` vs `:232`）：
   Excel 的 `BA` 公式用 `AS` 列（`v.get('hlto_charge', 40000)`），但控制台/`AI报价对比` sheet 的汇总值用硬编码 `40000`。一旦核验 JSON 里覆盖了 `hlto_charge`，**Excel 里和汇总表里的总价会对不上**。

2. **Transfer 吨价与 benchmark 脱节**：`TON_RATE[('Transfer','Casting')] = 35000`，但样本显示 CPM 36~46k、SEA-TOOL 48k，文档自己也写着"待 S3 上调至 ~42k"。→ 会系统性低估 Transfer 报价约 17~20%。

3. **重量缺失 → 运费为 0**（`generate_offer.py:128-130`）：`_twt` 取不到时传 0 给运费计算，`batch_consolidated_cost` 按 0 重量分摊，这批运费被静默吞掉。→ 应改为"重量缺失则该行运费标记为待定 + ⚠"。

这三条不影响"方法论"的价值，但如果要拿它的脚本直接演示，需要先修。

---

## 11. 三句话总结

1. **工作流 = 提示词层（SKILL.md 路由 + SOP）+ 脚本层（确定性换算）+ 契约层（JSON/Excel 作为跳间接口）**，LLM 只负责判断，从不负责计算。
2. **定价 = 三层漏斗**：几何量算准（80% 的价值）→ 分档吨价 × 系数 → 费用叠加；最贵的资产不是公式，而是**字段级信任矩阵 + 反例库 + 供应商基准样本**。
3. **产出物 = 一份自解释的 Excel（公式化 + 三 sheet）**：客户改一个数全套重算，同时把"AI 错在哪""我们核验了什么""假设是什么"全写在文件里——可解释、可追溯、可校准。