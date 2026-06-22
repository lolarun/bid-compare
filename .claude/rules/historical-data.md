---
paths:
  - "docs/data/**/*"
  - "scripts/*excel*.py"
  - "scripts/*history*.py"
  - "apps/api/services/comparison.py"
  - "apps/api/services/statistics.py"
  - "apps/api/services/scoring.py"
  - "apps/api/services/supplier_recommend.py"
  - "apps/api/services/quote_filters.py"
  - "apps/api/services/import_service.py"
  - "apps/api/routes/invite.py"
  - "apps/api/models/quote.py"
  - "apps/api/models/material.py"
  - "apps/api/models/supplier.py"
---

# 历史采购价格与供应商证据规则

- 历史采购价格是受治理的数据产品，不是 CRUD 表。原始文件、清洗产物、manifest、导入批次和业务查询必须可追溯。
- 原始转换文件放 `docs/data/raw/` 或相应来源目录；清洗后可用数据放 `docs/data/curated/`。禁止覆盖客户原始 Excel。
- 测试、E2E、演示、草稿、被排除和未确认报价不得进入正式历史价格统计、供应商推荐或品牌证据。
- 所有历史价格、供应商召回、品牌召回和统计查询必须统一使用 `valid_quote_filters()` 或同等的集中口径。
- 候选供应商必须为 active；merged、inactive、测试供应商不得被推荐。
- 品牌证据分级：授权/代理证书高于历史中标或采购记录，高于报价文本自述。报价里出现品牌不能直接认定代理关系。
- 历史价格基准必须匹配可解释的同规格口径，至少考虑材料族、DN/规格、单位、税价口径；样本不足返回无基准，不得退化为全品类最低价。
- 比价、邀标、供应商匹配和品牌匹配通过业务服务读取历史数据，不得在路由中拼接临时查询。
- 导入必须 dry-run、守恒报告、批次隔离和可回滚；禁止把当前比价结果自动归档为正式历史。
- 详细流程以 `docs/design/11-历史采购价格治理与业务服务.md` 为准。
