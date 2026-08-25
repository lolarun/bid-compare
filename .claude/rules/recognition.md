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
  VL-direct 分支同样已切 PaddleOCR-VL（`intelligence/paddle_tender.py`，design/26
  P4 补，2026-08-13）：`pipeline.py::extract_tender`/`tender_pdf.py::extract_bidlist`
  的 VL-direct 回落分支直接调用 `paddle_ocr.submit_and_parse`，`cells` 矩阵拼装成
  招标专用 CSV 后复用 `vl_tender.build_tender_draft()`；封面标量/招标要求（品牌等）
  走 Paddle 每页自带的 `text`/`markdown` 纯文本二次抽取（`paddle_doc_meta.py`，
  doc-type-agnostic，报价侧同样在用——`recognize_quote_paddle` 的
  `text_call`/`requirements` 参数），不需要视觉调用。同样有 `MockProvider`
  `isinstance` 分支（两处调用点都有）保留既有集成测试可用。**qwen 尚未整体
  删除**：`tender_text_layer.py`（轨A，见下条）的招标要求抽取仍然调用
  `provider.vl_extract_csv`（qwen 视觉），还没切到 `paddle_doc_meta` 的纯文本
  路径——删除 qwen 前必须先补上。**第二条承重分支 2026-08-23 已经落地**：design/33
  的空格子补位（`gap_fill.get_production_filler`）同样走 `vl_extract_csv`，而 Paddle
  的两个替代变体都实测失败（design/33 §2.5）。所以"删除 qwen"**已经不是清理工作，
  而是换平台决策**，不要当收尾任务排期。
  `provider` 不具备 `vl_extract_csv` 时直接报错，不做能力探测后的静默降级
  （`pipeline.py` 的 `hasattr` 检查是防御性守卫，不是路径选择）。
- 招标文件另有**文档级**文字层直抽（`tender_text_layer.py`，docs/design/25 轨A）：
  原生 PDF 检测到可用文字层且清单表可确定性抽取时**整份**走直抽、完全不调用视觉
  模型；检测失败或抽取不可信时**整份**回落 VL-direct。这与上一条禁止的双路径不冲突
  ——被禁止的是"同一份文档内按表复杂度分流 + 能力探测静默降级"，允许的是文档级
  二选一且来源诚实标注（`input_mode="text_layer"`，不冒充 `vl_direct`）。不得把它
  演化成文档内混合抽取。
- **模型供应商由两个开关决定，不是散落在各调用点（docs/design/41，2026-08-24）。**
  文本类走 `domain_config.TEXT_CLIENT_VENDOR`、视觉类走 `VISION_CLIENT_VENDOR`
  （`'dashscope'`|`'mimo'`，**默认都是 dashscope**）。文本类的客户端一律从
  `services/llm_provider.get_text_client()` 取——**禁止在调用点写死模型名**
  （此前 `bid_insight` 写死 `"qwen-plus"`、`block_alignment` 默认参数又写死一次，
  换供应商要改四处、漏一处就造成"大部分切了、有一处还在老家"的分裂状态）。
  视觉类切 mimo 时走 `providers/mimo_vision.MimoVisionProvider`（OpenAI 兼容，
  跟 dashscope 的私有 SDK 协议不同，换 base_url 换不过去，必须另起实现）。
  两个开关**故意分开**：两类调用的失败后果和验证方式不同，捆在一起切等于逼人
  一次性接受两种风险。配了 mimo 却没有 `MIMO_API_KEY` 时**明确回落并记日志**，
  不静默降级。**embedding 无法迁移**——mimo 没有 embedding 接口，对齐兜底
  （`anchor_match._embed`）只能留在 dashscope，这是硬约束不是遗漏。
- **列→角色映射允许用模型，行→行对齐不允许（docs/design/40，2026-08-23 已实现）。**
  判"哪一列是数量/单价/合价/税率"是**表结构解释**，一张表问一次，且产出必须过
  `intelligence/column_roles.verify_roles` 这道确定性验证（数值列能否解析成数、
  名称列是不是文本、**同税基**下 `数量×单价≈合价` 闭不闭合）才准采纳——猜错有独立
  证据当场证伪，不会落库。顺序不可交换：**词表先跑、验证在中间、模型只在验不过时
  兜底**，已知形状因此零模型调用、完全离线可复现。
  **已知局限，不得含糊**：乘法可交换，验证器**抓不出"数量与单价对调"**。风险在接线层
  收窄（`tabular_ingestion._only_missing`：词表只是缺角色时，模型只准填空、不准改写
  词表已认出的角色），并以 `_doc_meta.column_source` 留痕，不是靠假装能验。
  **行对齐不走这条路**：数量序列已能确定性解到 100%（design/39），且行级错配没有
  独立证据可以证伪——那正是 CLAUDE.md「LLM 不得重排候选」守的东西。
- **空格子补位是上一条的唯一例外（docs/design/33，2026-08-22 用户批准，
  2026-08-23 已实现：`intelligence/gap_fill.py`，**默认关闭**、由调用方注入
  filler，`gap_filler=None` 时七份快照回放指标逐字节不变）。** 允许在主路径**什么都没返回**
  的格子上跑第二个模型，四个条件同时成立才算数，缺一条就不是这个例外：
  ① 只补 `AMOUNT_EMPTY`（读不到），**不得碰任何已有值**——覆盖已识别值是 CLAUDE.md §4
  明禁的另一回事；② 逐字段标来源 `field_sources[field]="llm"`，不冒充 `direct_cell`；
  ③ 补出来的行必须过算术恒等式（数量×单价≈合价 / 合价×税率≈税额 / 合价×(1+税率)≈
  价税合计，凡输入齐全的都要过、且至少过一条），过不了**丢弃不入库**——留空是诚实状态，
  自洽不了的数字不是；④ **质量分级不因补位上抬**，补完仍是 REVIEW，仍要人工确认。
  被禁的仍然是"按表复杂度分流 + 能力探测静默降级"那种路由——主路径无条件、不因补位改变，
  这是例外成立的前提。实测依据（Paddle 自己补不了、方向错会返回格式完整的错值、算术门
  9/9 过 vs 0/9 挡）见 design/33 §2。
- 持久化标签值是 `"vl_direct"`，与模块名 `vl_quote.py` 不一致是**有意的**（评审 N1：
  模块 2026-08-11 改名，存量 `job.result` 里的标签不迁移）。三个键同义、都指这条
  识别路径：`fields.parser_mode` / `PageMetric.input_mode` / `meta.recognizer`。
  新代码判断识别来源时认 `"vl_direct"` 这个值，不要"顺手统一"改成 `vl_quote`——
  那是数据迁移，不是改名。
- Paddle 提交参数 `merge_tables` 的生产默认值是 **False**（2026-08-22，7 份报价件
  + 2 份招标件实测，见 `providers/paddle_ocr.py::submit_and_parse` 的对照表）。开着
  的时候 Paddle 把跨页续表整段行塞进 `begin` 那一页，续页的行**全部继承错误页码**，
  `source_ref` 从此说的是错页；关掉后每页独立成表，页归属在源头就对，召回同等或更好
  （绵存 87→89 行、宏胜 132→136 行）。改回 True 必须先拿出新的实测，不能凭"跨页表
  应该合并"的直觉。万一某份文档 Paddle 仍然合并，`paddle_vl._merged_page_spans()` →
  `SourceRef.page_end` 会把页码如实标成区间（"第 7-8 页"），**不得退回把 begin 页
  当确定事实**。
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
