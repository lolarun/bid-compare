---
paths:
  - "apps/api/routes/analysis.py"
  - "apps/api/routes/quotes.py"
  - "apps/api/routes/export.py"
  - "apps/api/services/anchor_match.py"
  - "apps/api/services/bid_matrix.py"
  - "apps/api/services/bid_insight.py"
  - "apps/api/services/bid_alignment.py"
  - "apps/api/services/bid_submission_resolve.py"
  - "apps/api/services/supplier_fill_llm.py"
  - "apps/api/services/evaluation_policy.py"
  - "apps/api/services/quote_readiness.py"
  - "apps/api/schemas/analysis.py"
  - "apps/api/models/bid_submission.py"
  - "apps/api/models/bid_alignment.py"
  - "apps/api/models/tender_list_session.py"
  - "apps/api/models/alignment_finalization.py"
---

# 招标比价后端规则

- `TenderAnchor` 是矩阵行主轴，`BidSubmission.id` 是供应商报价列身份。新接口不得用 `supplier_id` 代替 submission identity。
- 路由只负责鉴权、参数解析、事务边界和响应映射；匹配、质量、矩阵、评标政策和推荐逻辑放入独立业务服务。
- 所有比价入口必须显式解析当前且已确认的 tender session 与 active submissions；禁止静默选旧 session、旧 submission 或供应商历史报价。
- 顺序对齐只能作为经过完整性、数量、单位、规格和局部冲突门禁后的策略；冲突行必须单独 pending，禁止全表强制覆盖。
- 页面矩阵、Excel 导出和 AI 解释必须消费同一矩阵服务结果，不能各自重算业务口径。
- 评标政策必须来自招标文件或人工确认。未识别到评标法、权重或授标方式时返回 unknown，不得默认最低价中标、合理低价法、单一授标或自造权重。
- LLM 只能解释确定性计算结果、证据缺口和风险，不得自行改变候选排序、拆单、定标或补造评审事实。
- `checksum=fail`、严重完整性缺口或无有效报价可 BLOCKED；unknown 只能作为风险提示。conditional 可以展示候选和 AI 解释，但不能完成最终采购确认。
- pending、review_candidate、excluded 和无可靠价格口径的行不得参与评标总价、偏差、异常和推荐。
- 业务阈值集中配置并命名；禁止散落 magic numbers。API 字段命名必须区分 `submission_id`、`supplier_id`、`material_id` 和 `anchor_id`。
- 写操作必须幂等、可审计；批量确认需保存来源、原值、修正值、校验标记和操作者。
