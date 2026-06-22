# MEMPAS 工程待办（后端架构 / 技术债）

> 最后更新：2026-06-22（P1-6/P2-3 完成，P1-3+P1-4 合并待设计先行）
> 范围：后端架构整改与技术债。**产品功能 / UI / 客户反馈待办见 [`docs/TODO.md`](docs/TODO.md)**，两者不重叠。
> 权威依据：[`docs/design/12-招标比价后端审计与整改.md`](docs/design/12-招标比价后端审计与整改.md)（下称「审计12」）。
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

- [ ] **P1-3 识别审计字段弱类型 JSON**（§4 P1-3）
  缺口：`row_type` / `corrections` 未完整持久化到 `BidQuoteLine`；source_ref/flags/人工修正均为弱类型 JSON；缺字段级前后值与操作者。
  建议：高频质量字段结构化，JSON 留作原始审计包；人工修正另建 append-only change log。与 §11.4 `row_type` 双枚举同源。

- [ ] **P1-4 OperationLog 不足以满足字段级审计**（§4 P1-4，`models/operation_log.py:9-19`）
  现仅 user/module/action/target/result/remark/time，无结构化 before/after、无 project/session/submission/row identity。
  建议：新增领域审计事件（确认 / 修正 / 排除 / 重匹配 / finalize / 历史导入各记结构化 payload）。
  设计参考 `docs/design/14`；倾向**扩展 OperationLog 而非新建死表**（见 memory `project_p1_services`）。

- [x] **P1-6 业务阈值散落**（§4 P1-6）—— **2026-06-22 完成**
  domain_config.py 现收录全部10个 `MATCH_*` 领域阈值（含 `MATCH_SEQUENTIAL_SIM_THRESHOLD`、
  `MATCH_ARITHMETIC_PASS_THRESHOLD`、`MATCH_PRICE_ARITHMETIC_TOLERANCE`）。
  env 层（PAGE_CONCURRENCY / PDF_RENDER_CONCURRENCY / MAX_PAGES 等）有意保留在 env，符合三层模型，不属于错位。

> P1-1（7 服务）/ P1-2（submission 身份权威解析）/ P1-5（current confirmed session 统一）/ P1-6（阈值收口）已完成。

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

- [ ] **§10.3 `bid_matrix.py` 拆分**：矩阵 + 评标 `_evaluate_cell` + 推荐门禁 `_compute_recommendation` + 基准 + 族归一混在一处。
  拆 `bid_evaluation.py` / `bid_recommendation.py` / `matrix_cell.py`。这是 P1-1 BidMatrixService 留下的「第三批」尾巴。

- [ ] **§10.4 / §10.5 状态三轴枚举统一**（标注为「最高 ROI 的命名整改」，但范围大）
  同一三态被写成 7 套词汇且大小写不一（`BLOCKED` vs `blocked` 最易出 bug）。`status` 一词被生命周期 / 存在性 / 可评标性三条正交轴共用。
  方案：三轴各起名 `lifecycle_state` / `presence` / `evaluability`，集中 `core/enums.py`，三态主词收敛到 `AUTO/REVIEW/BLOCKED`。涉及 ORM 列 / API 契约 / 前端，需迁移 + 前端同步（审计12 归为第二/三批，因体量大延后）。

- [ ] **§11.2 软外键补正**：`bid_alignment.tender_list_session_id`、`BidAlignmentItem.submission_id`、`alignment_finalization.project_id`、`bid_matrix_version.project_id` 为裸 `Integer`，随 Alembic 补正式 `ForeignKey` 约束与索引。

- [ ] **§11.3 死代码 / 测试专用代码下线**：`pipeline.py:525 _assign_source_ref_from_grids`、`table_recognizer.py:931 _correct_page_orientation`、`splitter.PageSplitter` / `aggregator.ResultAggregator`（legacy `_run_batched` 路径）。全仓 grep 确认无生产调用后下线，**不得误删用户工作区资料**。

- [ ] **§11.4 `row_type` 双枚举统一**：`table_parser._classify_row`（header/note/empty）vs `table_recognizer._raw_items_to_draft_rows`（section_header/remark/invalid）同概念异名。并入 §10.4 定义单一 `RowType` Enum，并解决「未持久化到 BidQuoteLine、下游靠 `合计|小计` 正则兜底」。

- [~] **§11.5 横切重复代码** —— 部分完成
  已消除：`parse_id_csv()`（逗号分隔解析）、`get_finalization_snapshot()` / `get_current_confirmed_session()`。
  仍待抽：`OpenAI` 客户端三处各自实例化（`/bid-insight`、`/bid-alignment/suggest`、`/llm-fill`）→ LLM provider；聚合价 `round(agg_total/agg_qty,4)` 规则 5 处闭包复制 → 单一 helper；LLM 响应去 markdown 围栏 2 处逐字节重复 → 共用解析器。

- [ ] **§11.1 行级 bbox 来源证据**：`SourceRef.bbox` 全仓只读不写，行级定位覆盖率恒为 0。明确为**产品级持续目标（可下游回填）**，质量报告须如实标注「无行级像素定位」，不得宣称像素级完整追溯（见 memory `project_rootcause_layers`）。

- [ ] **§7.3 工作区卫生**：未跟踪的临时脚本（`scripts/` 大量 `??`）、`apps/api/services/rebuild_submission_lines.py`、重复 fixture 提交前分类归档或删除。**不得误删用户资料**。

---

## 4. 待客户 / 产品决策（非纯工程）

- [ ] **扩展属性权重比价**：客户要权重比价，但当前结构化数据仅品牌可用，需确认数据来源与口径（memory `project_attr_weight_gap`）。
- [ ] **Phase B-1 确定性 TableGrid shadow 切换**：shadow 检查点已验证（绵存 0 丢行等），待用户决定是否切 on（memory `project_phaseb_shadow`）。
- [ ] **比价基准 + 推荐重构**（同规格基准 / checksum 语义 / 三态门禁 / 确定性主供 / AI 只解释，4 规则）实施收尾确认（memory `project_baseline_recommendation_redesign`）。
- [ ] **sub18/sub19 数据修复未验收**：sub18 仅 124k vs 已知 1.07M，source_ref 仅 page 无 row（memory `project_sub1819_audit`）。属数据 / 识别缺口，非本轮架构债。

---

## 5. 已知非阻断现象（记录在案，非待办）

- `test_e2e_snapshot.py` / `test_compare_integration.py` 中 kaishuo / taikelong 共 7 个 snapshot replay 失败：既有缺页 fixture 所致，与近期改动无关（多轮 stash 基线复现确认，见 memory `project_baseline_metrics`）。补齐 fixture 或重录 snapshot 后方可转绿，不得静默跳页冒充。
