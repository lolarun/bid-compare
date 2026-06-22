# P0 设计：canonical valve_type family + risky tier rescue

## 目标
修两个确定性闸门误杀（不换模型/不改 prompt/不扩召回）：
1. `减压阀组 / 减压阀 / 可调式减压阀(组) / 小阻力可调式减压阀(组) / 小型可调式减压阀(组)` 被 canonical 当硬冲突
2. canonical 完全相等仍被 risky_canonical_conflict 强降 pending

保持：`false_positive_align_count = 0`；真子型/跨型冲突仍不放行
（橡胶瓣止回阀 ≠ 旋启/缓闭止回阀；球阀 ≠ 流量测试接口控制阀门；真空破坏器 ≠ 减压阀族）。

## 关键事实（决定设计）
- `SIM_THRESHOLD=0.50`，全部减压阀族 cell 的 cos=0.70~0.89，**远高于 0.50**。
  → 它们当前是 `risky_canonical_conflict`（c_score==0.0）而非 low_similarity。
  → **Fix1（family 评分>0）即可让它们在 `match_anchors_wide` 直接判 `safe`**，是主杠杆。
- 陷阱：`validate()` 与 `validate_anchor_fill()` 的 **g2 valve_type 闸门**会用原始文本
  重抽 `_q_vt` 并严格 `!=` 比较；若不改成 family 兼容，`减压阀` vs 锚点 `减压阀组`
  会被 g2 二次拦回 pending → **g2 必须同步改用 `valve_type_compatible`**。
- `extract_valve_canonical` 只产出 `减压阀组` / `减压阀` 两个关键词（其它描述词被吞掉），
  但 helper 仍按通用字符串设计，避免未来关键词扩充时失效。

## Fix 1 — canonical.py 新增 helper + 改评分

### normalize_valve_family(valve_type) -> str
- 显式 family 映射（仅收录“变体可互比”的类型）：
  `减压阀族` ⊇ {减压阀, 减压阀组}，外加兜底：`valve_type` 含子串 `减压阀` → `减压阀族`
  （捕获 可调式/小阻力/小型/比例式…减压阀(组)，且 `真空破坏器`/`流量测试` 不含“减压阀”不会误并）。
- **止回阀子型不归一**：不建立止回阀 family。`橡胶瓣止回阀`/`止回阀`/`旋启式…` 各自成族。
- 未命中 → 返回自身（独立族，绝不与他型兼容）。

### valve_type_compatible(anchor_type, quote_type) -> bool
- 任一为空 → True（通配，不阻断）
- 相等 → True
- `normalize_valve_family` 相等 **且** 属于已定义 family（非自身兜底）→ True
- 否则 → False

### canonical_match_score 调整
顺序：DN 冲突→0.0；PN 冲突→0.0；valve_type：
- 相等/通配 → 走满分路径
- 不等但 family 兼容 → 记 `vt_family=True`（**不再 0.0**）
- 不等且不兼容 → 0.0
返回：数据不足→0.5；family 兼容→**0.75**；精确匹配→1.0。
（Tier-1 仍要求 ==1.0，family 兼容不会被盲目自动对齐，交 LLM 复核——符合预期。）

## Fix 2 — supplier_fill_llm.py

1. import 增加 `valve_type_compatible`
2. `validate()` g2（~L297）：`_q_vt != _anchor_vt` → `not valve_type_compatible(_anchor_vt, _q_vt)`
3. `validate_anchor_fill()` g2（~L922）：同上
4. `validate()` risky 降级（L262-269）**rescue**：
   命中 risky 候选时，复算 `fresh = canonical_match_score(anchor.canonical, row.canonical)`：
   - `fresh >= 0.75`（精确或 family 兼容、且 DN/PN 不冲突）→ **不降级**，
     加 flag `family_normalized_verified`（精确匹配可不加）
   - 否则维持原行为：加 `risky_candidate:{tier}` 并降 pending
   说明：`>=0.75` 而非 `>0`，刻意排除 0.5（仅通配/数据不足）的弱匹配，避免过度放行。
   DN/PN 真冲突天然落在 `fresh==0.0`，不会被 rescue。

## 不变量（必须保持）
- true_subtype_conflict：valve_type_compatible=False → score 0.0 → 不 rescue、g2 仍拦 → pending
- 流量测试/真空破坏器：非阀族，incompatible → 拦截
- #28-31 凯硕 OCR 路径不触碰 → 仍 ocr_corrected_verified

## 测试
单测（test_pipeline_v24.py 扩展 + 可能新增 test_canonical_family.py）：
- score(减压阀组,减压阀)>0；score(小阻力可调式减压阀组,小型可调式减压阀)>0
- score(减压阀组,真空破坏器)==0；score(闸阀,流量测试接口控制阀门)==0
- score(橡胶瓣止回阀,旋启式止回阀)==0
- 完全相等 canonical 不产生 risky_canonical_conflict（tier=safe）
- valve_type_compatible 真值表
E2E：`python scripts/test_e2e_llm_fill.py --project 62 --category 阀门 --missing-audit --assert-regression`
验收：fp_count==0；quoted-only 66/90→~75/90；#70-74/#76-79 恢复；真子型不恢复；
重跑 audit_gate_misfire 后 normalization_false_kill 明显下降。
