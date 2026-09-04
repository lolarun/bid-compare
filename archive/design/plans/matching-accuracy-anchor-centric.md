# 比价匹配率修复 — OCR纠错 + anchor-centric填表（纵深纠错漏斗）

## Context（为什么做这个）

E2E「≥2家可比」封顶 ~56%（50/90）。用户质疑：三家都报了 ~100万阀门，怎么缺那么多？查**真实 Excel + DB + canonical 机制**，确认是**系统性匹配失败，非业务缺报**，根因两层、五种失败模式，全部数据验证：

**OCR 层（上游污染源）**：`RENDER_SCALE=2.0/MAX_EDGE_PX=2400`（[document_loader.py:25](apps/api/intelligence/document_loader.py:25)）对扫描小字偏保守；OCR 无领域词表（[dashscope_ocr.py:408](apps/api/intelligence/providers/dashscope_ocr.py:408)）；Stage-2「按原文」（[prompts.py:14](apps/api/intelligence/prompts.py:14)）→ 形近字写入 JSON 再污染 canonical。project 62 实测 27 行垃圾：

| 报价原文 | 真相 | canonical（实测） | 拦路虎 |
|---|---|---|---|
| `阀阀` ×13 | 闸阀 | vt=None，与闸阀锚点 score=**1.0** | **纯召回** — embedding 排候选外 + validate(b2) 禁选候选外 |
| `橡胶海止回阀` ×4 | 橡胶瓣止回阀 | 退化泛型`止回阀`，与`橡胶瓣止回阀`锚点 score=**0.0** | **召回+canonical 双杀** |
| `给排水` ×10 | 阀型全丢 | vt=None，只剩 DN | 需全清单顺序/材质判断 |

**匹配层（架构锁死能力）**：当前 LLM-fill **不是 anchor-centric，是"逐报价行从候选里选"** —— [build_prompt](apps/api/services/supplier_fill_llm.py:457) 按 quote 行喂候选，[validate(b2):222](apps/api/services/supplier_fill_llm.py:222) 丢弃候选外 seq。**召回是天花板**，LLM 结构上无权纠错；missing 无证据（[residue:332](apps/api/services/supplier_fill_llm.py:332) 只是"没被消费"）。

**已证伪可省**：锚点**已带** `materials`+`canonical.material`（#45-54给水=不锈钢/#55-64排水=PVC·球墨铸铁，已能区分）；重复系列靠报价**块结构/行序**区分（凯硕新正报价保留 DN40-200 + DN20-200 两段块）。→ **原 Fix B 锚点补材质不做**。

---

## 范式红线（贯穿全程，防止 quote-centric 惯性复发）

> **不要让 LLM 做"候选选择题"，要让 LLM 做采购员的"逐项填表题"。**

1. **候选只是参考，绝不是边界**。Top-K/hint 生成 **改纯 cosine，不做 canonical/DN 硬过滤**；只打标 `safe`(DN/PN/阀型/材质都顺) / `risky`(OCR疑似·阀型冲突·材质缺·组套表达不同) / `blocked`(强证据完全不同)，**risky 必须出现在 LLM 视野**，否则错别字能力永远用不上。
2. **LLM 看到该供应商完整报价清单（保留原始行序）**，逐采购项核对，而非逐报价行被动分配。召回不再是天花板。
3. **每个采购项必须有结论**，`missing` 必须带 `nearest_quote_candidates` 反证（含 `why_rejected`），系统不能轻易把"没匹配到"说成"缺报"。
4. **原文保真**：`raw_material` 永不覆盖，纠错落 `normalized_material` + `ocr_correction_reason`，匹配用 normalized，复核看得到依据。
5. **LLM 只提议，代码裁决，pending 给人复核**。价格永取真实 Quote；canonical 冲突/低置信 → pending，守住"0 冲突进 align"。

---

## 落地顺序 — 分两波（用户决策）

**Wave 1 = OCR 层**（Layer 0 + 1）：先把上游污染掐掉，独立可验证。
**Wave 2 = 匹配层**（Layer 3 + 4）：anchor-centric 填表 + missing 反证 + 嫌疑锚点 E2E。

每波结束跑验证再进下一波。基础设施项（含税单价正则、material_type、extraction_meta_json）**不是核心收益点**：material_type 与 extraction_meta_json **已完成**；含税单价正则（[tabular_ingestion.py](apps/api/services/tabular_ingestion.py) 末项 `(?<!合)单价` 未挡"不含税"）作为低优先 infra 顺手修，不阻塞主线。

---

## Wave 1

### Layer 0 — 渲染质量 A/B（最低改动）
**改** [document_loader.py:25-26](apps/api/intelligence/document_loader.py:25)：`RENDER_SCALE`/`MAX_EDGE_PX` 读 env（[config.py](apps/api/core/config.py) 加 `OCR_RENDER_SCALE=3.0`/`OCR_MAX_EDGE_PX=3600`），三处下采样（line 73/83）跟读。
**验证**：[scripts/test_qwen_ocr.py](scripts/test_qwen_ocr.py) 加 `--max-edge`，对凯硕/泰科龙关键页（橡胶瓣止回阀、闸阀、减压阀组所在页）跑 2400 vs 3600，人工确认形近字改善。瓶颈是 MAX_EDGE 下采样，非 dashscope 端（max_pixels≈2896² 已够）。

### Layer 1 — OCR 领域纠错（三层结构，取代硬编码词典）
复用现成 `material_type` 全链路打法（[quote_fact.py:119/153](apps/api/intelligence/quote_fact.py:119) 已通）。固定数据结构：
```json
{ "raw_material":"橡胶海止回阀", "normalized_material":"橡胶瓣止回阀",
  "canonical":{"valve_type":"橡胶瓣止回阀","dn":"DN50","pn":"PN16"},
  "ocr_correction_reason":"形近字，同页 DN50/65/80/100 连续，对应清单橡胶瓣止回阀连续项" }
```
- **1a prompt**（[QUOTE_PROMPT](apps/api/intelligence/prompts.py:37)）：保留原文到 raw_material；新增——发现明显形近字时填 normalized_material + ocr_correction_reason，**不覆盖原文**，纠错须基于阀门词表 + DN/PN + 相邻行连续性。注入合法词表（橡胶瓣止回阀/低阻力倒流防止器/节能消声止回阀/缓闭式止回阀/闸阀/球阀/蝶阀/截止阀/小阻力可调式减压阀组/Y型过滤器…）+ 例（橡胶脚·橡胶海+DN连续→橡胶瓣；阀阀+DN连续→闸阀）。
- **1b schema+QuoteFact**：[QUOTE_SCHEMA](apps/api/intelligence/schemas.py) item 加 `normalized_material`/`ocr_correction_reason`；[QuoteFact](apps/api/intelligence/quote_fact.py:98) 加同名字段并入 `to_item_dict()`。
- **1c canonical 用 normalized**：[build_canonical](apps/api/intelligence/quote_fact.py:43) 调用处（[pipeline._postprocess_quote](apps/api/intelligence/pipeline.py)）material 参数优先 `normalized_material or material`；build_canonical:62-65 已允许 LLM canonical 覆盖（hook 本在，prompt 之前禁用）。
- **1d 落库+前端不剥离**：[quotes.py batch-confirm](apps/api/routes/quotes.py) 写入 `Material.extended_attrs`（不新增列）；[client.ts](apps/www/src/api/client.ts)/[extraction.ts](apps/www/src/utils/extraction.ts)/[ExtractionEditor.vue](apps/www/src/components/ExtractionEditor.vue) hidden 白名单加两字段。

**Wave 1 验证**：重跑 3 份 PDF 抽取（现库 materials 烘进旧 OCR，必须重跑），确认 `阀阀→闸阀`、`橡胶海→橡胶瓣` 在 normalized_material 修正、canonical 不再 0.0 误杀。

---

## Wave 2

### Layer 3 — anchor-centric 填表（架构翻转）
重写 [supplier_fill_llm.py](apps/api/services/supplier_fill_llm.py) 填表策略，端点/mode 不变。

**hint 生成（落实红线①）**：新 `attach_nearest_hints` 走**纯 cosine** Top-N（无 canonical/DN 硬过滤），每候选打 `safe/risky/blocked` 标。**删** `match_anchors_wide` 的 0.0 剔除。normalized_material 喂 embedding → `阀阀`修正后`闸阀`自然召回。

**prompt**（`build_anchor_centric_fill_prompt`）：①90 锚点 `#seq|name|spec|[阀型/DN/PN/材质]|数量|备注`；②该家**完整报价清单、保留原始行序** `行序|quote_id|normalized(原文兜底)|规格|单价|数量`；③相似度提示（risky 也列，标⚠）；④OCR 词表。指令：**逐采购项填，候选仅参考，须扫全清单**。

**输出 schema（anchor-centric，固定逐项）**：
```json
{ "fills": [
  { "anchor_seq":28, "decision":"quoted|aggregated|pending|missing", "quote_ids":[13839],
    "confidence":0.82, "evidence":"报价第5页 橡胶海止回阀 DN50，疑橡胶瓣OCR误识，DN/数量一致",
    "ocr_correction":{"from":"橡胶海止回阀","to":"橡胶瓣止回阀"},
    "nearest_quote_candidates":[{"quote_id":13842,"text":"橡胶海止回阀 DN100","why_rejected":"..."}] } ] }
```
`missing` 必须给 `nearest_quote_candidates`（Top候选 + why_rejected）。

**validate v2**（扩展现有 [validate](apps/api/services/supplier_fill_llm.py:164)，不破不变量）：anchor-keyed→assignment 适配层，复用 quote_id 真实性/单次消费/价格取真实 Quote/agg 重算。**删 (b2) 候选边界**。canonical 改判（落实决策②分层口径）：有 ocr_correction → 用纠正名重算 canonical；一致 + **词表内标准词高置信** → quoted；一致但低置信/重复系列歧义 → pending+`ocr_corrected`；冲突(0.0) → pending+`canonical_conflict`（挂锚点可见）。`missing.nearest` 存供审计；真 missing 仅当 nearest 全弱。未消费 → residue。

**删除**（救候选法的死重）：[repair_pass](apps/api/services/supplier_fill_llm.py:632)、bolt-on [anchor_centric_pass](apps/api/services/supplier_fill_llm.py:733)、wide-recall tier 机器；[analysis.py](apps/api/routes/analysis.py) 编排同步简化。Tier-1 直判 v1 先去（一个机制更简单）。

### Layer 4 — E2E 钉死嫌疑锚点（落实点④）
[scripts/test_e2e_llm_fill.py](scripts/test_e2e_llm_fill.py) 保留总体阈值，**新增嫌疑锚点验收集**，每个锚点 ×三家供应商必须输出 `quoted/pending/missing + quote_id + 原始OCR文本 + 纠错文本 + 为什么不是missing`：

| 嫌疑锚点 | 期望 |
|---|---|
| #28-31 橡胶瓣止回阀 DN50-100 | 凯硕`橡胶海`→纠错对上，非 missing |
| #45-54 不锈钢给水闸阀 / #55-59 UPVC排水 / #60-64 球墨铸铁排水 | 凯硕两段`阀阀`块按行序分别落给水/排水系列 |
| #70-74 小阻力·可调式减压阀组 | 组套表达不同 → risky 候选可见，落 pending/quoted 非 missing |
| #83-87 青铜止回阀 DN20-50 | 与缓闭式/消声止回阀区分正确 |

并加**字段级准确率**（阀型+DN+PN+材质+数量+单价+行对应）抽样，替代字符级 96.8% 误导口径。

---

## 关键文件
- Wave1：[document_loader.py](apps/api/intelligence/document_loader.py:25)+[config.py](apps/api/core/config.py)；[prompts.py](apps/api/intelligence/prompts.py:37)+[schemas.py](apps/api/intelligence/schemas.py)+[quote_fact.py](apps/api/intelligence/quote_fact.py:98)+[pipeline.py](apps/api/intelligence/pipeline.py)+[quotes.py](apps/api/routes/quotes.py)；前端 [client.ts](apps/www/src/api/client.ts)/[extraction.ts](apps/www/src/utils/extraction.ts)/[ExtractionEditor.vue](apps/www/src/components/ExtractionEditor.vue)
- Wave2：[supplier_fill_llm.py](apps/api/services/supplier_fill_llm.py)（重写填表+validate v2+删死重）；[analysis.py](apps/api/routes/analysis.py)（编排简化）；[test_e2e_llm_fill.py](scripts/test_e2e_llm_fill.py)+[test_qwen_ocr.py](scripts/test_qwen_ocr.py)
- **不改**（冻结契约）：[bid_matrix.py](apps/api/services/bid_matrix.py)、[canonical.py](apps/api/services/canonical.py)、[match_anchors](apps/api/services/anchor_match.py)、batch-confirm 主流程
- **不做**：Fix B 锚点补材质（已带）

## 测试
- 单测：扩 [test_supplier_fill_validator.py](apps/api/tests/test_supplier_fill_validator.py)——anchor-keyed 输入、ocr_correction 重算 canonical、nearest 捕获、删边界后无幻觉（越界/重复/价格不符/冲突→pending）；纯 cosine hint 含 risky。Layer1：`build_canonical(normalized=...)` 纠错匹配、prompt 含词表。
- 不回归：[test_pipeline_v24.py](apps/api/tests/test_pipeline_v24.py)、[test_llm_fill_persistence.py](apps/api/tests/test_llm_fill_persistence.py)、[test_table_parser.py](apps/api/tests/test_table_parser.py)。

## 验证（E2E，服务固定 8002、禁 --reload）
- **Wave1**：① test_qwen_ocr 2400 vs 3600 形近字对比；② 重跑 3 PDF 抽取，确认 normalized_material/canonical 修正。
- **Wave2**：`python scripts/test_e2e_llm_fill.py --project <P> --category 阀门` —— 看 ≥2 可比率较 50/90 增益 + **嫌疑锚点表逐项三家明细**（13行阀阀+4行橡胶海被救回、#70-74 非 missing）。
- **门槛**：≥2 quoted+pending ≥70%、≥2 quoted ≥60%、0 canonical 冲突进 align、每个 missing 有反证、嫌疑锚点全部正确归类。
