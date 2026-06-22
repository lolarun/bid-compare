---
paths:
  - "apps/api/intelligence/**/*.py"
  - "apps/api/services/document_ingestion.py"
  - "apps/api/services/tender_pdf.py"
  - "apps/api/services/tender_list.py"
  - "apps/api/services/source_reconcile.py"
  - "apps/api/tests/test_*extraction*.py"
  - "apps/api/tests/test_table_*.py"
---

# 识别链路规则

- 识别必须覆盖文档实际页数；任何页数上限、失败页和角色过滤页都要分别报告，禁止静默截断。
- 页面角色分类使用视觉模型；规则只能做确定性硬判、校验和降级，不得以供应商名、固定页码或样本文件名决定角色。
- 表格结构优先使用 OCR HTML 与 TableGrid。TableGrid 不适用时允许 HTML + LLM fallback，但必须记录 parser mode、失败原因和质量指标。
- 不得重新引入已撤销的“所有表格必须确定性 TableGrid 直出 DraftRow”要求。结构可靠时可确定性取值，复杂表头和扫描件保留受控 LLM 路径。
- OCR、方向纠正、切片、LLM 抽取的输入输出必须可快照重放；fresh E2E 与 replay 测试必须明确区分。
- 生产 prompt 禁止出现真实供应商、项目、文件名、固定页码和样本专属列序；示例使用虚构占位符。
- 每行保留原始值、标准化值、来源页/表/行；可获得时保留 bbox 或 tile_bbox。没有 bbox 时不得宣称完整行级像素追溯。
- REVIEW 数据可以进入人工核对，但 pending/review_candidate 不得进入正式报价、匹配或比价；BLOCKED 禁止入库和下游计算。
- 数量、单价、合价、税率和税额只做校验与标记，禁止未经确认自动覆盖原值。
- PDF 渲染必须懒加载、分批释放；供应商任务可以并发，每个任务的页面并发必须受全局内存预算控制。
