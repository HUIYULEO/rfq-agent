# PR #1 评审报告（feat: compliance gate, shared amount rounding, business checks, human approval, analysis report）

> 评审对象：HUIYULEO/rfq-agent PR #1（提交 27febc7，基线 f1befff），已于 2026-11-03 15:20 合并为 main@19b0647。
> 本报告在合并完成后定稿；结论对合并后的 main 同样成立（27febc7 的树即当前 main 的树，PR 为单提交直系后代）。
> 验证方法：合并树完整提取实测（122/122 测试全绿）；六官方案例端到端复跑（A/B 数字逐项核对，含 22 个待确认项的 SR 3 / PMA 7）；金额口径探针；三个独立深审（合规门核心 / 批准入库与报告 / Web 层），各带针对性探针脚本（`runtime/pr1-review/_probe/`、`runtime/review_probes/`）。

## 总体结论

**内核质量高，声称几乎全部属实；有 3 个 🔴 建议尽快 fix-forward。**
近 40 条 PR 文本声称里，核实为真 33 条、轻度夸大 3 条、未发现虚报。上轮评审遗留的 C2（金额口径）、C1（导出语义）、"激活无开关"三项被正确修复。问题集中在两处：**人工复核门的"已答复"语义**（继承自基线、被本 PR 显著放大）与**批准入库的生命周期外壳**（幂等只靠一道可被 UI 击穿的状态墙）。合并本身不可怕：激活路径的两个 🔴 默认 fail-closed（`commercial_approval_enabled=False`），只有显式 `RFQ_DEMO_APPROVAL=1` 演示批准流程才会踩到；但它们都在"批准入库"这个商业门禁最不该松的位置，应在提交比赛正式版本前修掉。

## 已确认修复（对上轮评审）

| 上轮问题 | 处置 | 证据 |
|---|---|---|
| C2 金额勾稽比引擎严，分数数量整单误挡 | ✅ 修复正确且未弱化 | `policy.line_amount()` 统一 q2/HALF_UP 到分；探针：2.5 FT × 13.33 = 33.33 通过、33.325/33.32 均被拒；其余 6 类校验逐项复验仍触发 |
| C1 导出语义矛盾（供应商询价却弹"报价草案已生成"） | ✅ 修复 | 导出按钮按 quotable（complete + QUOTE/PARTIAL_QUOTE）分流文案；toast 如实描述"每单恒产分析报告"；导出产物变为"恒产 analysis-r{rev}.docx + 仅 quotable 时产报价草案" |
| 激活按钮永远失败且无开关 | ✅ 修复 | `_service()` 接入 `active_policy()`；默认无按钮（approval_enabled=False）；`RFQ_DEMO_APPROVAL=1` 显式开演示批准，产物双重标注「演示批准」 |

## 🔴 必修（合并后应尽快 fix-forward）

**R1. 多规则阻塞行：单条答案把"剩余问题"永久标记为已答复**（继承自基线，本 PR 新增 4 规则 + 4 个单条放宽答案后触发面从 1 组合扩到 C(4,2)）
- `pricing.py:572`（answered 只看 `resolutions[line_id]` 存在）+ `pricing.py:603-606` + `service.py:212-213`（answered 后拒绝再作答）。
- 机制：人工选了只放宽一条规则的答案（如 accept_noncert），行仍被剩余规则阻塞，但重建后的 RI 标 answered → `open_reviews()` 为空 → 任务 draft_ready / PARTIAL_QUOTE，剩余规则永不发问、再无作答入口——违背 compliance.py"never silently accepted"的核心承诺。
- 官方六案例不触发（A/B 多规则行在任一放宽后均有互补候选可解，实测全部 priced）；但平台宣称可拖入任意 .eml，真实询价很容易撞上。
- 修法：answered 判定校验已有答案是否命中当前 rules 的放宽项，或改为每规则一个 resolution key（如 `L019:moq_exceeds`），或 rules 变化即恢复 open。补一条多规则+单放宽的回归测试。

**R2. 激活后同 revision 再导出会击穿状态机**（需 `RFQ_DEMO_APPROVAL=1` 才可达）
- `export_quote_draft` 无 active 守卫（service.py:343-347）：active 任务再导出 → 状态降级为 exported、artifacts 被覆盖、已批准的 quote-r{rev}.docx 从下载列表消失；此时再批准 → 所有 ValueError 门禁放行 → 裸 `sqlite3.IntegrityError` 不在 web 的 except 分支 → 500，本 revision 永久卡死。探针已实测全链路复现。
- 修法：active 拒绝/短路 re-export；activate 进事务前按 `(task_id, revision)` 查重幂等；web 捕 IntegrityError 转 4xx；补测试。

**R3. 半提交窗口不可恢复**（需 `RFQ_DEMO_APPROVAL=1` 才可达）
- DB 事务（quote 行 + docx 渲染）提交后、`_save(task)`（task.json，事务外）失败 → 重试激活撞同一 UNIQUE 约束，只能手工 DELETE 数据库行恢复。探针实测。
- 修法与 R2 的幂等同源：查重 + INSERT OR IGNORE + 校验既有行一致；渲染 commit 失败时统一清理孤儿 docx（agent B 🟡3）。

## 🟡 建议修（择要）

1. 证书匹配大小写/空白敏感的精确子串（`"EASA FORM 1"`、`"FAA8130-3"` 变体会误判 cert_mismatch）；与要求侧（upper 匹配）不对称——`compliance.py:82-85`。
2. `required_certificates` 否定盲区：`"NO CERT REQUIRED"` 会凭空造出双证要求，点名 CoC 反而比不提更差——`compliance.py:71`。
3. `is_special_requirement` 否定盲区（`"NOT SPECIAL"` → True）——`compliance.py:76-79`。
4. `quote_with_moq` 定价成功后 decision_reasons 字面谎报不等式（"MOQ 50 ≤ 需求 10"，实际是 MOQ 50 > 需求 10 并按 MOQ 起订）——`pricing.py:347`。这条会写进询价分析报告。
5. `_catalog_unit` 同料号多单位时取第一行——`pricing.py:72-77`（演示数据无此情况）。
6. quote_history 落库只写不读（全库零消费者）——`db.py:322-331`。
7. 测试缺口：R1/R2/R3 三个边界、证书写法变体、`pma_requires_review` 关闭后"仍选 PMA 源"断言。
8. **上轮未清的旧账**：C4（前端 refresh()/boot() 仍无错误处理，一处端点 5xx 全屏空白）、C3（terms.py:89 合规矛盾文案 + test_terms.py:48 断言锁死）、D1（EVIDENCE.md 四处已过期陈述 + 新增功能带来的新漂移：策略键表、批准流程、"五种状态"图、README 历史口径）、导出后 inbox 无条件 mark_stage("draft")、evaluation_incomplete pill 无中文映射、evaluation_issues 前端零展示、MONEY_RULES 常量过时、save_upload 死代码（基线即有）。
9. GitHub 门禁的暴露面口径：CERT REQUIRED 与件号归一匹配在六官方案例上均为"未触发的防御性路径"（A/B 的 436 个候选全部 exact 匹配；D 是唯一带 CERT REQUIRED 的案例且无任何候选）——PR 文本未区分这一点，对外演示时如实说明反而是加分项。

## 核实为真的大项（不需要动）

- **宣称全部成立**：Line 新字段链路完整（HTML/xlsx 八列解析实测、PDF 行不含这些列为合理行为）；CERT REQUIRED 只认 EASA Form 1/FAA 8130-3、PMA/CoC 判 cert_mismatch、库存无证书判 cert_unverified；SR 生成 RI-xxxx-SR、最低价选 PMA 生成 4 选项 RI-xxxx-PMA（A 实测 3 SR + 7 PMA，编号稳定）；单位按原文比较——**对六案例零扰动**（A 的 6 条 IN 行与成本表 IN 记录精确匹配、B 的 golden 逐字段一致、PDF 保留 "Each" 原文）；件号精确→归一→别名分层 + 非精确必须 confirm_match；pma_requires_review 策略键真实读取且关闭有测试。
- **business_rules / analysis / 事务**：5 规则逐行 pass/warn/fail/not_applicable 条数精确（A:610、B:105…0 fail 0 漏行）；定位链四容器完整；fail 只进 priced 行（定向探针验证）；渲染失败 DB 回滚实测成立；analysis 每单输出、标题与内容不含"报价单"；demo/正式双模式 + auto_draft 不可背书 + system:* 拒绝全部有测试。

## 建议的 fix-forward 组合（PR #2 草案）

1. **激活生命周期**（R2+R3+孤儿清理+web 捕获+3 测试）：约 1–2 小时，改动集中在 service.py export/activate + web.py except。
2. **复核门 answered 语义**（R1+回归测试）：约 1–2 小时，改动集中在 pricing.build_review_items / service.resolve_review。
3. **一行级快赢打包**：terms.py:89 文案 + test_terms.py:48 断言（C3）、pricing.py:347 MOQ 文案、money_rules 常量同步、save_upload 删除：约 30 分钟。
4. 文档刷新（D1 + 状态图 + README 口径）单独一个 docs 提交：约 1 小时。

1+3 最先做（演示险区 + 几乎零成本）；2 紧随；4 收尾时做。

## 工作区收敛（比修 bug 更优先的"根"）

本地 F:\Codex\MatrixNEXT 与 GitHub（含本 PR 及此后的 main）是**两套不共祖的历史**，本地这份从此再也无法正常 pull。建议：

1. `git clone https://github.com/HUIYULEO/rfq-agent.git <新目录>`（例如 F:\Codex\MatrixNEXT\rfq-agent-main）；
2. 把以下内容**手工**拷入新克隆（它们被 .gitignore 挡在 GitHub 之外，我已实测仓库里没有）：`data/`（询价素材，372KB+）、`.deps/`（docx/lxml 依赖缓存，没有它 8 个 docx 用例跳过）、`.env`（本地密钥，若有）；
3. 老目录只留作素材库/档案，**不再对它做任何 git 操作**；今后你 + Codex 都只用新克隆。

此文件在 rfq-agent 工作区内，未提交 git；两个探针产物目录（runtime/pr1-review/）在 runtime/ 下，已被既有 .gitignore 覆盖。
