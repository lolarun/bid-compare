# 比价 E2E 测试文档清单（金桥地铁上盖 J9A-03 阀门项目）

> **2026-08-26（design/43）**：本文件已被 `tests/fixtures/documents/MANIFEST.md`
> 取代为当前权威语料清单（18 文件、A/B/C 三场景、design/28 起维护）。本文件保留
> 作为该 3-供应商子集当初的详细核对记录（页数/文字层类型/已知风险），下表路径
> 已随 design/43 的扁平化改名更新，但**新增语料请只更新 MANIFEST.md**。
>
> `docs/项目资料/初始资料/` 下曾有重复原件（仅作来源备份），design/43 已删除
> ——`tests/fixtures/documents/` 现在是**唯一副本**。

本文件记录"当前资料完整交付"这条 E2E 的最初基准测试集。所有识别准确率、质量门、对齐与导出的验收都以 `tests/fixtures/documents/`（design/28 起从 `docs/test/` 迁移，见同目录 `MANIFEST.md`）为准。

> 用途：作为回归 fixture（CLAUDE.md §10 允许的"真实回归样本"），不是项目专用硬编码。

## 文档集

| 角色 | 文件 | 路径 | 页数/大小 | 标准答案(ground truth) |
|---|---|---|---|---|
| 招标文件 | 金桥地体上盖项目-招标文件.pdf | `tests/fixtures/documents/金桥地体上盖项目-招标文件.pdf` | 18 页 / 0.30 MB | ✅ 见下方 Excel 清单 |
| 采购清单 | 金桥地体上盖项目-采购清单.xlsx | `tests/fixtures/documents/金桥地体上盖项目-采购清单.xlsx` | — | ✅ **本身即标准答案**；用户已核对，与招标 PDF ~100% 一致 |
| 投标-1 | 金桥地体上盖项目-上海绵存投标文件.pdf | `tests/fixtures/documents/金桥地体上盖项目-上海绵存投标文件.pdf` | **31 页** / 17.50 MB | ✅ 见下方 Excel 清单 |
| 投标-2 | 金桥地体上盖项目-泰科龙投标文件.pdf | `tests/fixtures/documents/金桥地体上盖项目-泰科龙投标文件.pdf` | **53 页** / 35.19 MB | ✅ 见下方 Excel 清单（扫描+转置表，最难）|
| 投标-3 | 金桥地体上盖项目-凯硕新正投标文件.pdf | `tests/fixtures/documents/金桥地体上盖项目-凯硕新正投标文件.pdf` | 19 页 / 14.88 MB | ✅ 见下方 Excel 清单 |

> 招标清单另有 `.xls` 版本（75 KB）同目录，内容相同；测试以用户指定的 `.xlsx` 为准。

### PDF 类型（文本层探测，2026-06-19）

| 文档 | 类型 | 含义 |
|---|---|---|
| 招标文件 PDF | **文本型**（约 11771 字） | 可直接抽取，Excel 标准答案可信 |
| 上海绵存(31p) | **扫描型，无文字层** | 全程依赖 OCR |
| 泰科龙(53p) | **扫描型，无文字层** | 全程依赖 OCR（且转置表，最难） |
| 凯硕新正(19p) | **扫描型，无文字层** | 全程依赖 OCR |

**三份报价均为扫描件，无文字层。** 数据层成败完全取决于"扫描件 OCR + TableGrid"；三份标准答案都需人工核对 OCR 结果，无文本直抽捷径。

## 标准答案 Excel（已全部完成，见 MANIFEST.md）

三份投标 PDF 均已有逐行核对过的标准答案 Excel：

- [x] 上海绵存投标文件 → `tests/fixtures/documents/金桥地体上盖项目-上海绵存报价清单.xlsx`
- [x] 泰科龙投标文件 → `tests/fixtures/documents/金桥地体上盖项目-泰科龙报价清单.xlsx`（89行，声明总价 1,067,616.41，用户逐行核对）
- [x] 凯硕新正投标文件 → `tests/fixtures/documents/金桥地体上盖项目-凯硕新正报价清单.xlsx`

## ⚠️ 已知风险（量化基线，待修）

- **页数截断**：报价页疑似上限 30 页（Codex 指出）。**泰科龙 53 页、上海绵存 31 页均超限**，存在静默漏页风险——这是数据层第一个要修且收益最直接的点。凯硕 19 页、招标 18 页在限内。
- 泰科龙为**扫描件 + 转置表**，整页 OCR 在第 11-12 页出现幻觉，第 13-14 页此前因截断未抽取。

## 已有线索（不是标准答案，仅供参考/做 diff 基线）

这些是**当前识别输出**，可用作"识别准确率 diff"的被测对象，但**不得当作标准答案**：

- `outputs/recognition_facts/{泰科龙,凯硕新正,上海绵存}_table_rows.csv` 等 —— 既有识别结果
- `data/two_stage/*__two_stage.csv`、`data/ocr_test/*__ocr.txt` —— 早期 OCR/两阶段输出
- `outputs/submission_reextract_audit/taikelong_reconcile.csv` —— 泰科龙 89 行人工核对参照表（约 14 行已确认值，含已知声明总价 ≈ 1,067,616.41），**可作为泰科龙标准答案 Excel 的起点**

## 验收口径（摘自 CLAUDE.md §14.2 / §13）

从原始文件重跑（不复用现有 DB 结果），硬性条件：PDF 总页数 = 已处理页数；确认报价行 source_ref(page/table/row/bbox) 覆盖率 100%；无小计/总计污染；有声明总价时金额闭环或有人工确认记录；BQL 行数 = 确认行数；`used_submission_ids` = 本次三份报价；矩阵行数 = 采购清单行数；pending=0 才锁定；页面矩阵与 Excel 导出逐格一致；不新建 Supplier/Material/Quote。
