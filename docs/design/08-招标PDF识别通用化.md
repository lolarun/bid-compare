# 08 — 招标 PDF 识别通用化设计

> 状态：设计稿，待讨论后实施

## 背景与问题

当前 `apps/api/services/tender_pdf.py` 存在明显的**阀门/给排水特化**问题：

| 现象 | 具体代码 |
|---|---|
| 页评分硬编码阀门关键词 | `_score_page`：`工作压力` / `阀体` / `密封圈` / `DN\d` 等 |
| 错误提示写死阀门字段 | `raise ValueError("含 序号/项目名称/工作压力/材质")` |
| 锚点构建调用阀门标准化 | `extract_valve_canonical(...)` |
| 材质字段假设五列固定 | `阀体/阀芯/阀板/阀杆/密封圈` |
| 抽取 prompt 只描述阀门格式 | `TENDER_BIDLIST_PROMPT` |

这意味着上传其他品类（桥架、配电箱、管材、水泵、风口风阀）的招标文件会：
- 页评分几乎全 0 → 定位失败
- 如侥幸进入抽取，LLM 被阀门 prompt 误导 → 字段错位

## 目标

能正确处理**至少 6 类**招标清单格式，每类精度不低于阀门当前水平（89 行，seq 0 缺失，source_ref 100%）。

---

## 一、通用页评分

### 现状

```python
def _score_page(html):
    if "工作压力" in html: bs += 0.3   # 阀门专属
    if any(kw in html for kw in ("阀体", "密封圈")): bs += 0.2  # 阀门专属
    if _DN_RE.search(html): bs += 0.2   # 阀门专属
```

### 目标

基础分（品类无关）+ 品类加权分（用于 category_guess，不用于 bidlist 门控）：

```
bidlist_base_score  = Σ 通用信号权重
  通用信号：
    序号列    +0.35   "序号" | "编号" | "No." (有表格且第一列是序号模式)
    名称列    +0.25   "项目名称" | "材料名称" | "设备名称" | "品名"
    规格列    +0.15   "规格" | "型号" | "参数"
    单位数量  +0.15   "单位" AND "数量"
    价格列    +0.10   "单价" | "综合单价" | "含税价"（有则加分，无则不扣）
    备注/品牌 +0.05   "备注" | "品牌" | "厂家"
    跨页续表  +0.20   "续" | "（续）" | 无表头但有 <table> (前序页已识别为清单)
    有 <table>+0.20
  门控阈值：bidlist_base_score >= 0.30（比当前 0.35 略宽松）

category_score   = 品类关键词计数（仅用于 category_guess，不影响 bidlist 门控）
```

输出每页：
```json
{
  "page": 14,
  "bidlist_score": 0.85,
  "brand_score": 0.10,
  "category_guess": "valve",
  "category_confidence": 0.9
}
```

---

## 二、品类识别

### 品类关键词表

| category_id | 中文名 | 关键词（任意命中即计分） |
|---|---|---|
| `valve` | 阀门 | 工作压力、阀体、密封圈、阀芯、阀杆、DN+数字 |
| `cable_tray` | 桥架 | 桥架、线槽、托盘、弯通、直通段 |
| `panel` | 配电箱/柜 | 配电箱、配电柜、断路器、开关柜、进线柜 |
| `pipe` | 管材管件 | 管材、管件、弯头、三通、法兰、PPR、镀锌 |
| `pump` | 水泵 | 水泵、离心泵、流量、扬程、功率(kW) |
| `hvac_diffuser` | 风口风阀 | 风口、风阀、散流器、百叶、风量 |
| `generic` | 通用兜底 | （以上均未命中时） |

识别策略：对 bidlist 页的 HTML 计算各 category_id 的命中关键词数量，归一化后取最高分。多品类清单：命中前两名分差 < 0.2 则标记 `multi_category=True`。

---

## 三、按品类选择 schema/prompt

### 现状

只有一个 `TENDER_BIDLIST_PROMPT`，描述的是阀门格式（含材质五子列、工作压力等）。

### 目标设计

```
CATEGORY_PROMPT_MAP = {
    "valve":         TENDER_BIDLIST_PROMPT_VALVE,
    "cable_tray":    TENDER_BIDLIST_PROMPT_CABLE_TRAY,
    "panel":         TENDER_BIDLIST_PROMPT_PANEL,
    "pipe":          TENDER_BIDLIST_PROMPT_PIPE,
    "pump":          TENDER_BIDLIST_PROMPT_PUMP,
    "hvac_diffuser": TENDER_BIDLIST_PROMPT_HVAC,
    "generic":       TENDER_BIDLIST_PROMPT_GENERIC,
}

CATEGORY_SCHEMA_MAP = {
    "valve":         TENDER_BIDLIST_SCHEMA_VALVE,      # 含 materials 五子列
    "cable_tray":    TENDER_BIDLIST_SCHEMA_CABLE_TRAY, # 含 material/surface_treatment
    "panel":         TENDER_BIDLIST_SCHEMA_PANEL,      # 含 rated_current/voltage/poles
    "pipe":          TENDER_BIDLIST_SCHEMA_PIPE,       # 含 material/connection_type
    "pump":          TENDER_BIDLIST_SCHEMA_PUMP,       # 含 flow/head/power
    "hvac_diffuser": TENDER_BIDLIST_SCHEMA_HVAC,       # 含 airflow/size
    "generic":       TENDER_BIDLIST_SCHEMA_GENERIC,    # spec 字段兜底，不解析子列
}
```

`generic` schema 设计：所有表格列以 `extra_fields: {列名: 值}` 形式收纳，不强制解析，保证不丢数据。

### 品类检测 → prompt 选择流程

```
page_htmls
  → _detect_category(page_htmls, bidlist_pages)  # 基于关键词计数
    → category_id, category_confidence, field_mapping_hint
  → prompt = CATEGORY_PROMPT_MAP[category_id]
  → schema = CATEGORY_SCHEMA_MAP[category_id]
  → LLM(prompt, schema, llm_input)
```

若 `category_confidence < 0.5` → 自动降级到 `generic`，并在 quality_metrics 记录 `low_confidence_category: true`。

---

## 四、人工确认页码与字段映射覆盖

### 问题场景

- 自动评分错过续表页（无表头）
- 用户文件有非标表头（如"品目编码"而非"序号"）

### API 接口扩展

现有 `extract_bidlist(bidlist_pages=None, brand_page=None)` 已支持手动覆盖页码。补充：

```python
def extract_bidlist(
    ...
    bidlist_pages: list[int] | None = None,   # 已有：手动指定清单页
    brand_page: int | None = None,            # 已有：手动指定品牌表页
    category_override: str | None = None,     # 新增：强制品类（跳过自动识别）
    field_map_override: dict | None = None,   # 新增：列名重映射 {"品目编码": "seq"}
):
```

前端 UI（Step 2 PDF 上传后）增加可折叠"高级设置"：
- 页码范围手动输入（现已有）
- 品类下拉（valve / cable_tray / panel / pipe / pump / hvac_diffuser / generic）
- 字段映射表格（列名 → 标准字段，仅在 field_mapping_confidence < 0.7 时展开）

---

## 五、逐页诊断输出扩展

### 现状

```json
{
  "page": 14,
  "input_mode": "html_fallback",
  "fallback_reason": "duplicate_headers",
  "expected_rows": 20,
  "extracted_rows": 19,
  "thinking_retry": false
}
```

### 目标

```json
{
  "page": 14,
  "input_mode": "html_fallback",
  "fallback_reason": "duplicate_headers",
  "expected_rows": 20,
  "extracted_rows": 19,
  "thinking_retry": false,
  "page_score": 0.85,
  "category_guess": "valve",
  "category_confidence": 0.92,
  "field_mapping_confidence": 0.78,
  "low_confidence_fields": ["materials.阀杆"]
}
```

`field_mapping_confidence` = 标准字段被命中的比例（已知字段数 / 期望字段数）。

---

## 六、影响文件清单

| 文件 | 变更类型 | 说明 |
|---|---|---|
| `apps/api/services/tender_pdf.py` | 重构 | `_score_page` 通用化；`_detect_category`；`extract_bidlist` 新增 category_override/field_map_override 参数；`_row_to_anchor` 按品类分支 |
| `apps/api/intelligence/prompts.py` | 扩展 | 新增 6 个品类 prompt（可分文件管理） |
| `apps/api/intelligence/schemas.py` | 扩展 | 新增 6 个品类 schema |
| `apps/api/services/canonical.py` | 扩展 | 现有 `extract_valve_canonical` 保留；新增按品类分发的 `extract_canonical(category, ...)` |
| `apps/api/routes/analysis.py` | 扩展 | `GET /analysis/tender-list/pdf-job/{id}` 返回新增诊断字段；考虑新增 `POST /analysis/tender-list/pdf-reextract`（带 override 参数）|
| `apps/www/src/api/client.ts` | 扩展 | `PageDiagnostic` 新增 `page_score / category_guess / category_confidence / field_mapping_confidence` |
| `apps/www/src/views/compare/IndexView.vue` | 扩展 | 页诊断展示加 page_score/category_guess；低置信度时展示字段映射确认 UI |

---

## 七、测试覆盖计划

| 场景 | fixture | 期望 |
|---|---|---|
| 阀门清单（当前）| 金桥招标文件.pdf | category=valve，89行，seq无缺失 |
| 无 DN 但有规格型号 | 需补充桥架/配电箱 fixture | category=cable_tray 或 panel，seq 无缺失 |
| 多品类清单 | 综合招标文件（含阀门+管材） | multi_category=True，按页分品类识别 |
| 续表页无表头 | 超过4页的清单 | 续表页评分 ≥ 0.30（通用基础分），不漏行 |
| Excel 存在额外行但 PDF 为主 | 金桥现有样本 | reconcile.recommended_source=pdf，only_in_excel_reference=['90'] |
| OCR 质量差/旋转页 | 扫描质量差的 PDF | page_score < 0.30 → 跳过 or 人工确认 |

---

## 八、实施顺序建议

1. **`_score_page` 通用化**（不改接口，只改权重）→ 最快见效，不影响阀门精度
2. **`_detect_category`**（新函数）→ 给 page_diagnostics 加 category_guess
3. **`generic` prompt/schema**（兜底）→ 让其他品类至少能出结果
4. **品类 prompt 逐个落地**：先桥架（最高优先级？），再配电箱，依次类推
5. **人工确认 UI**（`field_map_override`）→ 最后，属于保险措施

不要步骤 1-3 没完成就上步骤 4；每步完成后要用真实 PDF fixture 验证，不能光靠 unit test。

---

## 九、不做的事

- **不拆分 tender_pdf.py 成多文件**（除非行数 > 600）：过早拆分增加调用链复杂度
- **不在阀门 prompt 里加 if/else 品类分支**：品类数 ≥ 3 时一定要独立 prompt，否则 token 浪费且 LLM 容易混淆
- **不用通用 LLM 做字段自动映射**（太贵、太慢）：字段映射置信度低时展示给用户手动确认，LLM 仅在确认后辅助验证
