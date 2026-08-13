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

- **视觉识别**整份文档一次性渲染、整份送视觉模型一次调用，CSV → `ExtractionDraft`，
  legacy 逐页 OCR→HTML→TableGrid→LLM 链路已物理删除（2026-08-11，最佳实践评审
  F1/F2）。**报价侧**（quote）唯一路径是 PaddleOCR-VL（`intelligence/paddle_vl.py` +
  `providers/paddle_ocr.py`，design/26 P4，2026-08-13 起）：`pipeline.py::extract_quote`
  直接、无条件调用 `paddle_ocr.submit_and_parse`，不经过 `LLMProvider` 抽象——这不是
  "能力探测后选路径"，是唯一路径本身换了实现；`cells` 矩阵拼装成规范 CSV 后复用
  `vl_quote.build_draft()` 做 CSV→`ExtractionDraft`（`vl_quote.py` 在报价侧收缩为纯
  CSV 解析器，不再持有视觉调用）。`extract_quote` 里对 `MockProvider` 的
  `isinstance` 分支是唯一例外，且只服务测试替身（35 个既有集成测试依赖
  `MockProvider.vl_extract_csv` 产出报价数据）——按类名识别一个自我声明的测试
  桩，不是探测真实生产引擎的能力后静默降级，两者不可混同。**招标侧**（tender）
  仍走 `intelligence/vl_tender.py`，经 `LLMProvider.vl_extract_csv`（DashScope/qwen）
  ——design/26 全程只评估了报价侧，招标侧未验证 Paddle 前不得下线或改写这条路径。
  `provider` 不具备 `vl_extract_csv` 时直接报错，不做能力探测后的静默降级
  （`pipeline.py` 的 `hasattr` 检查是防御性守卫，不是路径选择）。
- 招标文件另有**文档级**文字层直抽（`tender_text_layer.py`，docs/design/25 轨A）：
  原生 PDF 检测到可用文字层且清单表可确定性抽取时**整份**走直抽、完全不调用视觉
  模型；检测失败或抽取不可信时**整份**回落 VL-direct。这与上一条禁止的双路径不冲突
  ——被禁止的是"同一份文档内按表复杂度分流 + 能力探测静默降级"，允许的是文档级
  二选一且来源诚实标注（`input_mode="text_layer"`，不冒充 `vl_direct`）。不得把它
  演化成文档内混合抽取。
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
