# 物料主数据与历史采购数据复核报告

**日期**: 2026-07-10  
**范围**: data/mempas.db + docs/data/ + docs/项目资料/  
**结论**: 物料主数据确实由采购记录直接推导，存在结构性对齐问题

---

## 一、数据链路现状

```
docs/项目资料/材料汇总/*.xlsx (10 个 Excel)
  ↓ build_raw_assets.py (SHA256-pinned)
docs/data/raw/2026-06-23/csv/
  ↓ import_historical.py + analyze_data.py parsers
materials (8,303 行) ← 1:1 → quotes (8,303 行)
  ↓ refresh_material_baselines()
materials.ref_price_* (单点价格, 非分布)
```

**设计意图** (doc/01 + doc/11):
```
Excel → raw/ → curated/v1/ (清洗+校验+审核) → materials (标准化) → quotes (多对一)
                                                    ↑                    ↑
                                              物料编码体系           多条报价聚合到同一物料
```

**实际**: raw → materials+quotes 直接写入，**curated 步骤完全跳过**。

---

## 二、核心问题

### 问题 1: 物料与报价 1:1 — 违反设计

| 指标 | 设计意图 | 实际 |
|------|---------|------|
| 物料:报价 | 1:N (一个物料多条历史报价) | **1:1** (8,303 = 8,303) |
| ref_price_low / high | P10 / P90 百分位 | **low == high** (100%) |
| supplier_count | 去重计数 | 0 或 1 (**永远 ≤1**) |
| price_cv | 标准差/均值 | **无意义** (单点) |

每个物料只有一条报价、一个价格、一个"供应商"。价格基线无法做 IQR 过滤、无法计算偏差阈值。

### 问题 2: 363 行真正重复物料

同 `category + standard_name + spec + unit` 的物料被创建多次：

| 重复次数 | 物料 | 问题 |
|---------|------|------|
| 45x | 潜水泵 \| 导杆 \| 32\*1.2 \| 台 | 应为 1 个物料 45 条报价 |
| 45x | 潜水泵 \| 浮球 \| / \| 台 | 同上 |
| 44x | 潜水泵 \| 链条 \| 6mm \| 台 | 同上 |
| 34x | 配电箱 \| 门禁电源配电箱 \| \| 台 | 同上 |
| 19x | 阀门 \| 闸阀 \| DN100 \| 个 | 同上 |
| 18x | 风口风阀 \| 风阀 \| 630\*320 \| 个 | 同上 |

**根因**: `import_historical.py` 的 dedup cache 按 `(category, standard_name, spec, unit)` 去重，但汇总 sheet 每个项目一列、同一物料重复出现时，可能因空白字符/编码差异导致 cache miss。

### 问题 3: 供应商完全未关联

| 指标 | 值 |
|------|-----|
| quotes.supplier_id IS NOT NULL | **0 / 8,303** |
| materials.brand 非空 | **0 / 8,303** |
| quotes.brand 非空 | 6,013 / 8,303 |
| quotes.brand 值类型 | 混合：短品牌名(扬州新扬) + 全公司名(江苏华威线路设备集团有限公司) |

**设计** (doc/11 §2): "Supplier names and brand names in raw files must not automatically create or merge supplier master data."

**实际**: 品牌名以文本形式存在 quotes.brand，未标准化、未关联 suppliers 表（103 条，来自其他渠道）。

### 问题 4: 物料名称未标准化

设计 (doc/01 §2.1): standard_name 应来自"标准模板库"。

实际: standard_name 是 Excel 原文直入，如 "给排水器DN20PN16YZ"（OCR 错误混入名称）、"15KW"（功率值当名称）等。这些不是标准化名称。

### 问题 5: curated 层缺失

设计 (doc/11 §3): `docs/data/curated/v1/` 应包含清洗、分类、校验后的候选事实。

实际: `docs/data/curated/` 目录**不存在**。raw 数据直接进入生产库。

---

## 三、数据来源清单

| source_id | Excel 文件 | 品类 | 报价数 | 专业 |
|-----------|-----------|------|--------|------|
| air-vent | 0风口风阀报价单格式.xls | 风口风阀 | 3,099 | 暖通 |
| electrical-panel-2026-05-20 | 0配电箱.xlsx (用户反馈版) | 配电箱 | 2,219 | 电气 |
| valve | 0阀门询价格式.xls | 阀门 | 913 | 给排水 |
| bridge-tray | 0桥架报价单格式模板.xls | 桥架 | 662 | 电气 |
| ss-pipe | 0不锈钢管清单.xlsx | 不锈钢管 | 580 | 给排水 |
| submersible-pump | 0潜水泵询价格式.xlsx | 潜水泵 | 557 | 给排水 |
| busbar | 0母线报价单格式模板.xls | 母线槽 | 138 | 电气 |
| fan-coil | 0风盘报价单格式.xls | 风机盘管 | 75 | 暖通 |
| hvac-pump | 0空调泵询价格式 .xlsx | 空调泵 | 46 | 暖通 |
| water-tank | 0水箱报价清单.xlsx | 水箱 | 14 | 给排水 |
| **合计** | | **10 品类** | **8,303** | |

所有报价共享 batch_id = `hist-v1-2026-06-23`，quote_date 全部为空。

---

## 四、修复方案

### 阶段 1: 去重合并（数据层，低风险）

**目标**: 将 363 行重复物料合并为 1 个物料 + N 条报价。

**方法**:
1. 查询所有 `(category, standard_name, spec, unit)` 重复组
2. 保留每组中 id 最小的物料作为主记录
3. 将重复物料的 quotes 关联更新到主物料
4. 删除重复物料记录
5. 重新计算 `refresh_material_baselines()`

**预期**: 8,303 → ~7,940 物料，多出的报价聚合后产生真正的价格分布。

### 阶段 2: 物料-报价重新聚合（数据层，中风险）

**目标**: 将同物料多报价聚合，使 ref_price_low/high 成为真正的 P10/P90。

**方法**:
1. 定义更宽松的聚合键（如 `category + 标准化名称 + 规格 + 单位`，标准化 = 去空格 + 统一大小写 + 规格归一化）
2. 合并同聚合键的物料
3. 重新计算价格统计（P10/P50/P90、CV、supplier_count）
4. 修复 `deviation_threshold`（基于 CV 自动设定）

**风险**: 规格归一化可能误合并不同规格（如 DN100 vs DN100 PN16）。需要逐品类测试。

### 阶段 3: 供应商关联（数据层，中风险）

**目标**: 将 quotes.brand 关联到 suppliers 表。

**方法**:
1. 从 quotes.brand 提取所有唯一品牌/公司名
2. 用别名表（ALIASES in import_brands.py）标准化
3. 在 suppliers 表查找或创建对应记录
4. 回填 quotes.supplier_id
5. 更新 materials.supplier_count 和 recommended_brands

**风险**: 品牌名与供应商名混淆（如 "KITZ" 是品牌，"开滋流体控制(上海)有限公司" 是供应商）。需要人工审核映射。

### 阶段 4: 物料名称标准化（业务层，需人工）

**目标**: 将 standard_name 从 Excel 原文改为标准名称。

**方法**:
1. 导出所有 distinct standard_name + category
2. 人工审核，建立"标准名称映射表"
3. 脚本批量更新
4. 清理异常名称（如 "15KW"、"给排水器DN20PN16YZ"）

**需要**: 采购/业务人员参与定义标准名称。

### 阶段 5: curated 层建立（架构层，高投入）

**目标**: 按 doc/11 设计建立 `docs/data/curated/v1/` 数据审核流程。

**内容**:
- 清洗后的候选事实
- 分类校验报告
- 被拒绝的记录及原因
- 物料映射表

**需要**: 新建 curation 脚本 + 审核流程设计。

---

## 五、优先级

| 阶段 | 紧迫度 | 难度 | 依赖 |
|------|--------|------|------|
| 1. 去重合并 | ★★★★★ | 低 | 无 |
| 2. 物料-报价聚合 | ★★★★☆ | 中 | 阶段 1 |
| 3. 供应商关联 | ★★★☆☆ | 中 | 别名表完善 |
| 4. 名称标准化 | ★★☆☆☆ | 高 | 需人工定义标准 |
| 5. curated 层 | ★★☆☆☆ | 高 | 阶段 1-4 完成 |
