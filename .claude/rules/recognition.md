---
paths:
  - "apps/api/intelligence/**/*.py"
  - "apps/api/services/ingestion/document_ingestion.py"
  - "apps/api/services/tender/tender_pdf.py"
  - "apps/api/services/tender/tender_list.py"
  - "apps/api/services/tender/source_reconcile.py"
  - "apps/api/tests/test_vl_*.py"
  - "apps/api/tests/test_cable_golden.py"
  - "apps/api/tests/test_tender_pdf_extract.py"
---

# 识别链路规则

- 报价与招标识别的唯一路径是 VL-direct（`intelligence/vl_quote.py` / `vl_tender.py`）：
  整份文档一次性渲染、整份送视觉模型一次调用，CSV → `ExtractionDraft`。legacy 逐页
  OCR→HTML→TableGrid→LLM 链路已物理删除（2026-08-11，最佳实践评审 F1/F2）。不得以任何
  理由重新描述或引入"部分表格走确定性 TableGrid、复杂表头走 LLM fallback"的双路径
  架构——`provider` 不具备 `vl_extract_csv` 时直接报错，不做能力探测后的静默降级
  （`pipeline.py` 的两处 `hasattr` 检查是防御性守卫，不是路径选择）。
- 持久化标签值是 `"vl_direct"`，与模块名 `vl_quote.py` 不一致是**有意的**（评审 N1：
  模块 2026-08-11 改名，存量 `job.result` 里的标签不迁移）。三个键同义、都指这条
  识别路径：`fields.parser_mode` / `PageMetric.input_mode` / `meta.recognizer`。
  新代码判断识别来源时认 `"vl_direct"` 这个值，不要"顺手统一"改成 `vl_quote`——
  那是数据迁移，不是改名。
- 识别必须覆盖文档实际页数；页数上限、方向未定页和丢行都要分别报告（`row_ledger`/
  `orientation_unresolved`，doc/19 §L3），禁止静默截断。
- pdfium 不是线程安全的：所有渲染入口（含只读的 `get_page_count`）必须整体串行经过
  `document_loader.py` 的 `_PDF_LOCK`。不得为提升并发把它拆成信号量或跨线程调用——
  崩溃发生在原生层，Python 侧只看到一个不可复现定位的 `OSError`。
- 每行保留原始值、标准化值、来源页/表/行（`source_ref`，由识别侧行输出直接构造，
  不再经过表格网格的二次定位查找）；可获得时保留 `bbox`/`tile_bbox`，没有 bbox 时
  不得宣称完整行级像素追溯。
- OCR、方向纠正、切片、LLM 抽取的输入输出必须可快照重放；fresh E2E 与 replay 测试
  必须明确区分，不得互相冒充。
- 生产 prompt 禁止出现真实供应商、项目、文件名、固定页码和样本专属列序；示例使用
  虚构占位符（`.claude/rules/recognition.md` 自身也遵守这条）。
- REVIEW 数据可以进入人工核对，但 pending/review_candidate 不得进入正式报价、匹配
  或比价；BLOCKED 禁止入库和下游计算。
- 数量、单价、合价、税率和税额只做校验与标记（`validation_flags`/`arith_suggested_qty`），
  禁止未经确认自动覆盖原值；"原文明确不报价"（`not_quoted`）与"读不到"必须分开标记，
  不得合并成同一个空值语义。
- `document_row_index`（全局文档行序）与 `page_row_index`（页内行序）是顺序直连对齐
  和定向重读的唯一输入；识别与后处理任一层丢失都会让下游静默退回载入顺序，必须逐行
  随行传递，不得在中间层被过滤或重算。
