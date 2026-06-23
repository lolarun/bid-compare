# 报价识别 E2E 完整测试方案

目标:把招标侧已验证的「真实 OCR → 抽取 → 与标准答案 Excel 对账 + 质量门」模式,**完整搬到报价侧**,让报价识别的准确率从"凭感觉"变成"可测量、可回归、可验收"。基准文档见 [E2E_FIXTURES.md](E2E_FIXTURES.md)。

本方案不替代、不降低 CLAUDE.md §6/§13/§14 的任何要求;它是把那些要求在报价侧**变成可执行的断言**。

---

## 0. 现状盘点(真实代码)

| 已有 | 文件 | 覆盖 |
|---|---|---|
| 结构单元测试(**无 API**,用缓存 OCR) | `apps/api/tests/test_real_ocr_fixtures.py` | `table_parser` 把 `含税合计/详见投标清单/1067616.41` 等正确判为 `grand_total`,不入商品行 |
| 流程集成(**合成数据**) | `apps/api/tests/test_bql_e2e.py` | batch-confirm→BQL→match→anchor-review→matrix→archive 的数据流不变量(不建 Quote、只走 BQL、used_submission_ids) |
| 招标侧真实 OCR e2e(**标杆**) | `apps/api/tests/test_tender_pdf_extract.py::test_extract_bidlist_real_pdf` | 真实 PDF→抽取→quality_metrics→与 Excel 对账 |

**缺口(本方案要补的)**

1. **没有"报价 PDF → 真实 OCR → 行 → 与标准答案 Excel 对账"的 e2e**(招标有,报价没有)。
2. **报价路径不产出质量报告**:`pipeline.extract_quote()`(`intelligence/pipeline.py:95`)只返回 items,无 `_compute_quality_metrics` 那样的覆盖率/seq/财务闭环指标(招标侧 `services/tender_pdf.py` 有)。→ 数据层没有刻度。
3. ~~**静默截断无信号**~~:已修复 — `MAX_QUOTE_TABLE_PAGES=30` 限制已移除,`_run_with_roles` 现在处理所有 QUOTE_TABLE 页,无上限。
4. **source_ref 无 bbox**:`_assign_source_ref_from_grids`(`pipeline.py:578`)只产出 `{page,table,row}`,无 bbox(§5 生产目标证据缺失)。

---

## 1. 前置依赖(没有它测不了)

- **3 份报价的标准答案 Excel**(逐行人工核对,见 E2E_FIXTURES「待办」)。字段:序号/名称/规格/品牌/单位/数量/不含税单价/含税单价/税率/不含税合价/含税合价/来源页。
  - 泰科龙起点:`outputs/submission_reextract_audit/taikelong_reconcile.csv`(声明总价 ≈ 1,067,616.41)。
  - 凯硕、绵存:声明总价需从投标书封面/汇总页确认(凯硕曾见 932,154.00 / 26,017.13 差额)。
- 缓存 OCR(供 L1 无 API 测试):`data/ocr_test/{泰科龙,凯硕新正,上海绵存}投标文件__ocr.txt`(已存在)。

---

## 2. 四层测试方案

### L1 — 结构层单元测试(无 API,快,每次 CI 跑)

**对象**:`table_parser.html_to_table_grids` / `_classify_row` / `page_classifier.classify_page`,喂缓存 OCR HTML。
**在 `test_real_ocr_fixtures.py` 基础上扩展,三份报价各覆盖:**

- TableGrid 重建:**转置表**(泰科龙)行列还原、rowspan/colspan、多级表头、跨页续表合并。
- 行类型:`grand_total`/`subtotal`/`section_header`/`remark` 不被判为 `quote_line`(已部分覆盖,补全三份)。
- 价格口径:含税单价 vs 不含税单价分列正确;`qty×单价≈合价` 算术校验;13% 税率一致。
- 断言形态:对每份缓存 OCR,`quote_line` 行数、被排除的合计/小计行、关键大额行(如泰科龙 qty=198/242/399 项)逐项核对。

**通过标准**:结构层在缓存 OCR 上对三份报价的行类型分类 100% 正确(尤其合计行零污染),转置表能还原出独立商品行。

### L2 — 报价识别真实 OCR e2e(每份报价 PDF 一个,标杆模式)

**对象**:`pipeline.extract_quote(pdf)` 全链真实 OCR;**新增** `test_quote_extract_real_pdf.py`,镜像招标侧 `test_extract_bidlist_real_pdf`。
**标记** `@pytest.mark.e2e` + skipif(无 PDF/Excel/KEY),CI 默认不跑,人工/夜间跑。
**对每份报价输出诊断报告并断言:**

- **页处理完整性**:渲染页数 = PDF 总页数(泰科龙 53/绵存 31/凯硕 19);所有 QUOTE_TABLE 页均处理,无上限截断——**禁止静默丢页**。
- **行级来源**:每条 `quote_line` 有 `source_ref{page,table,row}`,覆盖率 100%;(bbox 为生产目标,补齐后纳入断言)。
- **行集合对账(vs 标准答案 Excel)**:用 `source_reconcile.reconcile_anchors` 同款做行集合差异 + 字段比对;缺行/多行/字段不符逐条列出。
- **财务闭环**:明细 `Σ含税合价` 与声明总价差额 ≤ 配置阈值(泰科龙对 1,067,616.41);不闭环必须可解释。
- **口径**:含税/不含税不混用;税率一致;`qty×单价≈合价` 通过率 ≥ 阈值。
- **无污染**:合计/小计/标题/备注不出现在商品行。

**通过标准(REVIEW 档,对应 §14.2)**:页不漏、行级来源全覆盖、合计零污染、财务闭环、行集合与标准答案差异在阈值内。**不要求全自动读对每个值**——读不准的进人工,但必须被定位、被标记。

### L3 — 流程集成测试(合成 / 由已确认行驱动,无 API)

**对象**:`test_bql_e2e.py` 已有链路,补充断言:

- batch-confirm 只接受已确认行、只写 BidSubmission/BidQuoteLine,**不建 Quote/Material/Supplier**;写入行数 = 确认商品行数;合计/小计行被拒。
- match/anchor-review/matrix/export 全程以 `submission_id` 为列身份;`used_submission_ids` 精确等于本次集合;无历史 Quote fallback。
- **页面矩阵 ⨉ Excel 导出逐格一致**。

### L4 — 全链路真实验收(§14.2,从原始文件重跑,不复用 DB)

招标 PDF + 3 份报价 PDF,从上传开始重跑到矩阵+导出。**硬性验收(摘自 §14.2/§13 集成断言)**:

- 各文档总页数 = 已处理页数(无静默截断)。
- 确认报价行 source_ref 覆盖率 100%。
- 商品行无小计/总计污染。
- 有声明总价时金额闭环或有人工确认记录。
- BQL 行数 = 用户确认行数;`used_submission_ids` = 本次三份;矩阵行数 = 采购清单行数(89)。
- pending=0 才允许 finalize;不新建 Supplier/Material/Quote。
- 页面矩阵与 Excel 导出逐格一致。

---

## 3. 为支撑断言需要补的产品代码(测试驱动)

L2 要能断言,必须先给报价路径装上"刻度"。以下是测试驱动的最小生产改动(**另行评审,不在本方案内实现**):

1. **报价侧 DocumentQualityReport**:把招标侧 `tender_pdf._compute_quality_metrics` 的口径移植到 `extract_quote` 返回(source_ref 覆盖率、seq、算术一致率、含税/不含税一致性、声明总价差额、`truncated`)。
2. ~~**截断显式化**~~:已随 `MAX_QUOTE_TABLE_PAGES` 移除一并解决。
3. **bbox 进 source_ref**:`_assign_source_ref_from_grids` 透传 TableGrid 的 bbox(§5 生产目标证据)。

---

## 4. 验收阈值(集中配置,勿散落魔法数字)

| 指标 | L2 报价 REVIEW 档 | 来源 |
|---|---|---|
| 页处理覆盖 | 100%,截断必显式 | §3.1 |
| source_ref 覆盖率 | 100%(page/table/row;bbox 补齐后) | §5 |
| 合计/小计污染 | 0 | §6 |
| 算术一致率 `qty×单价≈合价` | ≥ 0.95 | §6 |
| 含税/不含税口径一致 | 一致 | §6 |
| 明细 vs 声明总价 | 差额 ≤ 5 元(跨级舍入合理误差) 或有人工说明 | §6/§14.2 |
| 行集合 vs 标准答案 | 缺/多行在阈值内,差异逐条列出 | 本方案 |

---

## 5. 执行命令

```powershell
# L1 结构单元(快,无 API)
python -m pytest apps/api/tests/test_real_ocr_fixtures.py -q

# L3 流程集成(无 API)
python -m pytest apps/api/tests/test_bql_e2e.py -q

# L2 报价真实 OCR e2e(需 DASHSCOPE_API_KEY + 标准答案 Excel,逐份)
python -m pytest apps/api/tests/test_quote_extract_real_pdf.py -s -m e2e -q

# 招标侧标杆(已通过,可对照)
python -m pytest "apps/api/tests/test_tender_pdf_extract.py::test_extract_bidlist_real_pdf" -s -m e2e -q
```

---

## 6. 落地顺序

1. **先做泰科龙标准答案 Excel**(最难、信息最全),作为 L2 第一个被测对象。
2. 补 L1 转置表/续表结构断言(用缓存 OCR,无成本)。
3. 补产品代码三项(质量报告/截断显式/bbox),让 L2 有东西可断言。
4. 写 L2 `test_quote_extract_real_pdf.py`,先跑泰科龙,再凯硕、绵存。
5. L4 全链路验收一次跑通。
