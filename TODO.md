# MEMPAS 工程待办（后端架构 / 技术债）

> 最后更新：2026-06-23（§7.3/§10.3/§10.4/§11.2/§11.3/§11.4/§11.5 完成；snapshot replay 7 失败修复）
> 范围：后端架构整改与技术债。**产品功能 / UI / 客户反馈待办见 [`docs/TODO.md`](docs/TODO.md)**，两者不重叠。
> 权威依据：[`docs/design/12-bid-backend-audit-remediation.md`](docs/design/12-bid-backend-audit-remediation.md)（下称「审计12」）。
> 本文件只列**已决定延后**或**未完成**的项；已交付项见各 commit 与审计12 §8。

---

## 0. 本次会话（2026-06-22）新增延后项

### 0.1 拆分 `routes/analysis.py`（2430 行 / 4 个域 / 错名）—— 方案 A（推荐，未执行）

`routes/analysis.py` 含 31 条路由，横跨 tender-list / anchor-review / bid-alignment / bid-matrix / 遗留分析看板 5 个关注点，文件名与 URL 前缀 `/api/analysis` 均已名实不符。

- **方案 A（纯文件拆分，保留 `/api/analysis` 前缀）—— 建议执行**
  对外零变更（URL 不动、前端不动、18 个 URL 测试不受影响）。风险低：仅 `routes/__init__.py` import 该 router；helper 无跨域共享。
  唯一需跟随处理：`test_bql_e2e.py` / `test_llm_fill_persistence.py` 直接 import 了 `_load_supplier_fill_rows` / `_persist_llm_fill`（6 行 import 需跟着搬）。
  建议切法（均保留 `prefix="/api/analysis"`）：
  | 新文件 | 路由 | 约行 |
  |---|---|---|
  | `analysis.py`（瘦身，遗留分析/看板） | `/compare` `/supplier-score` `/multi-compare` `/bid-insight` `/bid-matrix`(旧) `/category-stats` `/dashboard*` `/refresh-baselines` `/compare-state` | ~400 |
  | `tender_list.py` | `/tender-list/*`（match/confirm/preview/reconcile/current/versions/deactivate） | ~700 |
  | `tender_list_fill.py` | `/tender-list/llm-fill` + `_load_supplier_fill_rows`/`_persist_llm_fill`/`_select_suspect_anchor_seqs` | ~600 |
  | `anchor_review.py` | `/anchor-review/*` + `/bid-alignment/*` | ~600 |
  | `bid_matrix_versions.py` | `/bid-matrix/save` `/bid-matrix/versions*` | ~100 |

- **方案 B（改 URL 前缀 `/api/analysis/tender-list/*` → `/api/tender-list/*`）—— 记为债务，暂不做**
  这才是「真正修好名字」，但 URL 是前端消费的公开契约，改它 = 破坏性变更，需前后端联调。按「速战速决」原则暂不付这次 breaking change，错名作为已知债务留在前缀里。

### 0.2 抽离两个胖路由进 service —— 方案 C（设计先行，未立项）

P1-1 抽了 confirm/export/finalize 等 7 个服务，**唯独漏了 match 和 llm-fill**，业务逻辑仍压在路由里：

- `tender_list_match`（`analysis.py` ~407 行）：含 submission 三重硬闸门、session 解析、锚点重建、品牌上下文编排 → 抽 `MatchOrchestrationService`。
- `tender_list_llm_fill`（~364 行）+ `_load_supplier_fill_rows`/`_persist_llm_fill`（~590 行合计）：含 LLM 填表持久化 → 收口进 service。

二者是全文件最重、最危险的路由，属「非琐碎功能」，须走设计先行 + replay/回归，**不得塞进 0.1 的机械搬家**。纯文件拆分只是把胖肉挪个位置，`tender_list.py` 拆完仍约 1300 行——根因在这两个路由。

---

## 1. P1 剩余（审计12 §4）

- [x] **P1-3+P1-4 合并：字段级审计 + row_type 持久化** —— **2026-06-22 完成**（commit `bdaa890`）

  **P1-3 BidQuoteLine.row_type**：
  - 新列 `row_type VARCHAR(32)`（迁移 0003，存量回填 `quote_line`）
  - 词汇收口（§11.4 双枚举合并）：`quote_line|section_header|remark|invalid|subtotal|grand_total`
  - `header`→`section_header` / `note`→`remark` / `empty`→`invalid`
  - `confirm_batch` 落库时写入 `normalize_row_type(item.row_type)`

  **P1-4 OperationLog.payload 结构化事件**：
  - 新列 `operation_logs.payload JSON`（迁移 0003）
  - `services/audit.py`：`write_domain_event()`（no-commit，caller 控制事务）
  - 7 个事件埋点：`bql_confirm / tender_session_confirm / alignment_group_confirm /
    alignment_item_confirm / alignment_bulk_confirm / alignment_finalize / llm_fill_persist`
  - payload schema：`{event_type, identity, before, after, meta}`

  **未完成（超出本轮范围）**：
  - 人工修正的字段级 before/after（需要修正端点，当前无此 API）
  - corrections append-only 表（留待修正 API 建立后一起做）

- [x] **P1-6 业务阈值散落**（§4 P1-6）—— **2026-06-22 完成**
  domain_config.py 现收录全部10个 `MATCH_*` 领域阈值（含 `MATCH_SEQUENTIAL_SIM_THRESHOLD`、
  `MATCH_ARITHMETIC_PASS_THRESHOLD`、`MATCH_PRICE_ARITHMETIC_TOLERANCE`）。
  env 层（PAGE_CONCURRENCY / PDF_RENDER_CONCURRENCY / MAX_PAGES 等）有意保留在 env，符合三层模型，不属于错位。

> P1-1（7 服务）/ P1-2（submission 身份权威解析）/ P1-3+P1-4（row_type+审计事件）/ P1-5（session统一）/ P1-6（阈值收口）已完成。

---

## 2. P2 技术债（审计12 §5）

- [ ] **P2-2 领域归一化逻辑重复**（§5 P2-2）
  阀门 family/DN 逻辑散在 canonical service / anchor_match 粗分类 / prompt 规则三处口径。
  建议：建 `MaterialIdentityService`，统一输出 canonical family/DN/PN/unit + evidence/confidence。（亦见 §10.3 `canonical/standardize/enhance/category_classify` 四处收口）

- [x] **P2-3 匹配流程写 `Material.extended_attrs`**（§5 P2-3）—— **2026-06-22 完成**
  删除了 `anchor_match.import_and_match()` 中向 `Material.extended_attrs` 回写 canonical 的11行代码。
  `extract_valve_canonical()` 是纯函数，无IO，按需计算即可，无需DB缓存。

> P2-1（Alembic 版本化迁移）/ P2-3（anchor_match 写主数据副作用）已完成。

---

## 3. 第三批：结构治理（审计12 §7.3 / §10 / §11）

- [x] **§7.3 工作区卫生** —— **2026-06-23 完成**
  `.gitignore` 新增 `scripts/`、`tmp/`；`.claude/rules/`、`.claude/plans/`、根目录 `tests/` 已提交。

- [x] **§10.3 `bid_matrix.py` 拆分** —— **2026-06-23 完成**（commit `1bb54e2`）
  - `bid_evaluation.py`：`_evaluate_cell` + 4 帮助函数（`_anchor_spec/_canon_family/_pending_is_qty_only/_EVAL_QTY_TOL`）
  - `bid_recommendation.py`：`_compute_recommendation`
  - `bid_matrix.py`：1291→890 行，保留 re-export 供测试向后兼容
  - 剩余可做（低优先级）：`_build_cell_for_supplier` 抽 `matrix_cell.py`；两处 `agg_total/agg_qty` helper

- [x] **§10.4 状态三轴枚举统一（后端）** —— **2026-06-23 完成**（commit `7e8bb85`）
  - `core/enums.py`：`CELL_*`（cell status）+ `QG_*`（quality gate）+ `REC_*`（recommendation level）+ `RT_*`（row type）
  - `bid_matrix.py`：`_compute_recommendation` 改用 `REC_BLOCKED/REC_CONDITIONAL` 常量
  - **未完成**：ORM 列 / API 契约 / 前端枚举同步（涉及破坏性变更，需前后端联调，延后）

- [x] **§11.2 软外键补正** —— **2026-06-23 完成**（commit `295a536`）
  - Alembic 0004：`batch_alter_table` 添加 4 列 FK（bid_alignment_groups / bid_alignment_items / alignment_finalizations / bid_matrix_versions）
  - ORM 模型同步更新 `ForeignKey()` 声明

- [x] **§11.3 死代码 / 测试专用代码** —— **2026-06-23 决定保留**
  - `pipeline.py:522 _assign_source_ref_from_grids`：docstring 已标注「Used by tests. Production path now goes through table_recognizer.」
  - `table_recognizer.py:929 _correct_page_orientation`：docstring 已标注「生产路径已停用 ... 仅保留供 test_orientation_correction.py」
  - `_run_batched`、`PageSplitter`、`ResultAggregator`：经 grep 确认为活跃生产代码（§11.3 范围已修正，非死代码）
  - 决定：两个真实死函数保留作测试辅助，不删除（删除破坏 test_orientation_correction.py）

- [x] **§11.4 `row_type` 双枚举统一** —— **2026-06-23 完成**（commit `7e8bb85`）
  - `table_parser._classify_row` 返回规范词汇：`section_header/remark/invalid`（取代 `header/note/empty`）
  - `table_recognizer._raw_items_to_draft_rows` 过滤条件同步为 `RT_INVALID`
  - `services/audit.py normalize_row_type` 仍保留旧词汇→规范映射（接收前端/旧数据）

- [x] **§11.5 横切重复代码（LLM 客户端）** —— **2026-06-23 完成**（commit `7e8bb85`）
  - `services/llm_provider.get_dashscope_client()`：统一工厂，4 处实例化全部收口
  - **剩余债务**：`agg_total/agg_qty` 5 处 helper（在 bid_matrix.py 内，低优先级）；LLM 响应去 markdown 围栏 2 处（待 §0.1 拆分后顺手处理）

- [ ] **§11.1 行级 bbox 来源证据**：`SourceRef.bbox` 全仓只读不写，行级定位覆盖率恒为 0。明确为**产品级持续目标（可下游回填）**，质量报告须如实标注「无行级像素定位」，不得宣称像素级完整追溯（见 memory `project_rootcause_layers`）。

---

## 4. 待客户 / 产品决策（非纯工程）

- [ ] **扩展属性权重比价**：客户要权重比价，但当前结构化数据仅品牌可用，需确认数据来源与口径（memory `project_attr_weight_gap`）。
- [ ] **Phase B-1 确定性 TableGrid shadow 切换**：shadow 检查点已验证（绵存 0 丢行等），待用户决定是否切 on（memory `project_phaseb_shadow`）。
- [ ] **比价基准 + 推荐重构**（同规格基准 / checksum 语义 / 三态门禁 / 确定性主供 / AI 只解释，4 规则）实施收尾确认（memory `project_baseline_recommendation_redesign`）。
- [ ] **sub18/sub19 数据修复未验收**：sub18 仅 124k vs 已知 1.07M，source_ref 仅 page 无 row（memory `project_sub1819_audit`）。属数据 / 识别缺口，非本轮架构债。

- [ ] **qwen 去留：删除，还是保留为第三层像素重读兜底？——挂起，手工测试后讨论**（2026-08-13）

  **背景**：design/26 决策 #3 当前写的是"直接替换、无运行时兜底、同轮删除 qwen"，依据是用户 2026-08-13 的"我不需要兜底，太麻烦了，直接替换"。同日手测中用户澄清本意可能是三层链路（"PDF 不是走文字-Paddle-qwen 么"），该决策**现处于待复议状态，不得当作已确认前提执行删除**。

  **两个候选形态**：
  - A（现文档）：文字层 → Paddle → BLOCKED 即诚实终止（疑点收件箱 + Excel/重扫人工出路），qwen 全删，DashScope 依赖一并移除。
  - B（用户澄清）：文字层 → Paddle → **qwen 像素层重读** → 仍 BLOCKED 才终止。

  **若选 B，三条约束必须同时写死**（否则会退化成本仓库实测有害的形态，HANDOFF:301「LLM 文本复核会改坏真值」——模型把官方声明总价改成了抽错的值，闭环从此永远通过、缺口永久隐藏）：
  1. 触发条件是**确定性门的判定结果**（BLOCKED / 关键字段大面积缺失），不是"让 LLM 看看对不对"；
  2. qwen 的输入必须是**原始图像**，绝不能把 Paddle 抽好的 CSV 喂给它去"校正"；
  3. 换路必须有 `parser_mode` 标签，文档级二选一，不静默。

  **决策依赖的证据**：手测中 Paddle 判 BLOCKED 的实际发生率与形态。若极少发生 → A 更省（少一条链路、少一套依赖）；若常发生且 qwen 能救回 → B 划算。

  **牵连项**（选 A 才需要清；选 B 则不再是债）：
  - 轨A（`tender_text_layer.py`）的招标要求抽取仍调 `provider.vl_extract_csv`（qwen 视觉），见 `services/tender/tender_pdf.py:188-194`——qwen 目前唯一的生产调用点；
  - design/26 §10（qwen 删除计划整节）、`.claude/rules/recognition.md` 相应条目需跟着改。

---

## 5. 已知非阻断现象

（无——所有已知非阻断现象已消除。全套测试 506 passed 0 failed。）
