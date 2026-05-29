# MEMPAS TODO

> 最后更新：2026-05-29 · 当前版本：v0.2.1

---

## 用户反馈（2026-05-27）— 待排期

> 来源：`docs/项目资料/用户反馈/2026-05-27/mempas系统需求分析260527.docx`

### 🔴 Bug / 严重偏差

- [x] **Excel 批量导入按钮无响应** ✓ 2026-05-29
  `apps/www/src/views/import/IndexView.vue`
  已修复：下载模板按钮绑定 downloadTemplate()，OCR tab 改用 ExtractionEditor 组件。

- [x] **OCR 品牌识别错误且无法修改** ✓ 2026-05-29
  `apps/www/src/components/ExtractionEditor.vue`
  已修复：OCR 结果表改用 ExtractionEditor，所有字段（含品牌）均可编辑。

- [x] **OCR 识别出无关内容（钢管/电箱/水管等）** ✓ 2026-05-29
  ExtractionEditor 已支持逐行删除（Modal.confirm 防误操作）。

- [x] **导入后无日志** ✓ 2026-05-29
  compare 确认入库时已 toast 展示入库行数：`已入库 N 条报价`。

- [x] **识别入库按钮命名错误** ✓ 2026-05-29
  `apps/www/src/views/compare/IndexView.vue`
  已将"确认入库"改为"校对入库"。

### 🟡 重要功能缺失

- [x] **单家报价与历史价格比价功能** ✓ 2026-05-29
  `apps/www/src/views/compare/IndexView.vue` + `apps/api/services/bid_matrix.py`
  比价流程现在支持仅选 1 家供应商：步骤三展示该供应商报价 vs 历史均价/合理史低的对比。
  品牌档位规则已实现：全合资品牌时仅与合资历史价比较（`_detect_brand_tier_filter`），
  含国产品牌时与全品牌比较。StatCard/底栏/评估卡片均已适配单供应商模式。

- [x] **气泡图无数据不出图** ✓ 2026-05-29
  `apps/api/services/statistics.py` + `apps/www/src/views/dashboard/IndexView.vue`
  已修复：前端接入真实 heatmap/bubble API；气泡图和热力图无数据时显示 a-empty 占位；
  热力图格子显示金额标签；月份筛选器支持日期范围过滤（后端 bubble 端点新增 date_from/date_to）。

- [x] **招标比价：材料选择改为分阶段（专业 → 材料）** ✓ 2026-05-29
  `apps/www/src/views/compare/IndexView.vue`
  新增专业下拉（电气/给排水/暖通），选后品类下拉仅显示对应分类，选专业时清空品类。

- [x] **品牌档位命名改为国产 / 合资** ✓ 2026-05-29
  已统一更新：BrandTierModal、系统设置页、导入页、帮助中心、API 类型定义、后端模型注释。

- [x] **邀标建议移到招标比价之前（导航排序）** ✓ 2026-05-29
  `apps/www/src/router/index.ts` 已调整路由顺序。

- [x] **邀标建议：加入具体品牌要求** ✓ 2026-05-29
  `apps/www/src/views/invite/IndexView.vue` + `apps/api/services/supplier_recommend.py`
  推荐供应商卡片展示该供应商的历史品牌列表。新增"品牌要求"输入框，支持指定品牌名（如施耐德、ABB）。
  后端根据品牌匹配度加分（最高+20分），前端绿色高亮匹配品牌。

- [x] **历史比价增加起始时间筛选** ✓ 2026-05-29
  历史价格查询页已默认日期范围 2021-03-01 ~ 今天，去掉金额筛选，增加规格/材质筛选。

- [x] **未中标清单导入支持** ✓ 2026-05-29
  `apps/api/models/quote.py` + `apps/api/services/import_service.py` + `apps/www/src/views/compare/IndexView.vue`
  比价配置页新增"标记为未中标清单"复选框，勾选后导入的报价 bid_status='未中标'。
  后端 import/batch-confirm 端点均支持 bid_status 参数。
  热力图/气泡图查询排除 bid_status='未中标' 的报价；基线计算与邀标推荐仍包含。

### 🟢 体验优化 / 管理功能

- [x] **仪表盘布局调整（基础）** ✓ 2026-05-29
  已去掉"最近比价活动"和"高频材料价格趋势"模块；热力图与气泡图改为左右并排。
  ✓ 热力图格子显示价格、气泡图金额标注、起始时间筛选已完成。
  ⏳ 剩余：颜色对比、分层钻取。

- [x] **数据流（待办队列）简化版** ✓ 2026-05-29
  `apps/www/src/views/queue/IndexView.vue` + 路由 `/queue`
  展示 extraction_jobs 表：文件名、类型（招标/报价）、状态（排队/识别中/已完成/失败）、进度条、耗时、时间戳。
  支持按状态和类型筛选；识别中任务自动 5s 轮询。

- [x] **项目名称管理页签** ✓ 2026-05-29
  `apps/www/src/views/projects/IndexView.vue` + `apps/www/src/router/index.ts`
  新增项目管理页（CRUD）：表格展示、新建/编辑 Modal、删除确认、状态筛选、搜索。
  路由已从 `/projects → /compare` 重定向改为真实页面，侧边栏显示"项目管理"。
  ⏳ 剩余：合并项目功能、权限控制（维护员/比价员区分）。

- [x] **"采购数据分析"改名为"历史价格查询"** ✓ 2026-05-29
  已更新路由、侧边栏、页面标题、仪表盘快捷入口、操作日志模块名。
  筛选项已去掉金额、增加规格/材质。
  ✓ 导出按钮已改为仅管理员可见（v-if isAdmin，通过 useUserStore 判断角色）。

- [x] **供应商增加品类层级与厂家/供应商分类** ✓ 2026-05-29
  `apps/api/models/supplier.py` + `apps/www/src/views/suppliers/IndexView.vue`
  新增 `supplier_type` 字段（供应商/厂家），已加入模型、schema、TS 类型、表格列、画像 Drawer。
  品类层级：已有 `categories` JSON 数组字段，支持多品类归属。

- [x] **清单管理（管理员功能）** ✓ 2026-05-29
  `apps/www/src/views/batches/IndexView.vue` + `apps/api/routes/quotes.py`
  新增清单管理页：按 batch_id 分组展示已入库报价，显示批次编号、条数、供应商、项目、时间。
  后端新增 `GET /api/quotes/batches` 和 `DELETE /api/quotes/batches/{batch_id}`。
  ⏳ 剩余：修改所属项目、品牌信息编辑。

- [x] **物料主数据：命名规范整理** ✓ 2026-05-29
  `apps/api/services/standardize.py` + `apps/api/core/config.py`
  修复关键 bug：热浸镀锌 vs 热镀锌不再合并（价差 20%~50%）。
  新增别名：线槽/槽盒→槽式桥架、消防桥架→防火桥架、室外桥架→防水桥架。
  新增 NAMING_STANDARDS 参考数据（桥架/风口风阀命名维度）及 API 端点。

- [ ] **配电箱比价增加元器件维度**
  用户反馈配电箱比价应同时支持：
  1. 同编号箱子整体价格横向对比（已有）
  2. 元器件价格 + 箱体价格 + 各项报价系数对比（待实现）
  ⚠️ 注意：与 2026-05-20 客户反馈"取消 BOM 拆分"存在矛盾，需再次与客户确认。

- [x] **邀标建议：清单参数补全（如桥架板厚）** ✓ 2026-05-29
  `apps/api/intelligence/schemas.py` + `prompts.py` + `pipeline.py`
  TENDER_SCHEMA 新增 extended_attrs 对象字段，TENDER_PROMPT 列出 10 个品类的专属技术参数。
  _postprocess_tender 透传 extended_attrs（过滤空值），前端 ExtractionEditor 以蓝色 tag 展示。

---

## P1 — 近期必做

### 后端

- [x] **AI 报价对齐复核：建议生成接口** ✓ 2026-05-29
  `apps/api/routes/analysis.py` + `apps/api/services/bid_alignment.py`
  新增 `POST /api/analysis/bid-alignment/suggest`。
  输入同一项目的多供应商报价识别结果，调用大模型分析疑似未对齐行，输出建议合并组、置信度、原因、字段纠错建议。
  测试：20 行报价数据 → 8 组对齐建议 + 5 个字段纠错，4168 tokens / 52s。

- [x] **AI 报价对齐复核：用户确认落库** ✓ 2026-05-29
  `apps/api/models/bid_alignment.py` + `apps/api/routes/analysis.py`
  新增 `bid_alignment_groups` / `bid_alignment_items` 持久化结构。
  POST apply 保存确认分组，GET groups 查询，DELETE 撤销。
  不覆盖原始识别值，保证可追溯和可撤销。

- [x] **横向比价矩阵支持确认后的对齐方案** ✓ 2026-05-29
  `apps/api/services/bid_matrix.py`
  `bid_matrix` 优先按用户确认的 alignment group 聚合报价；没有确认方案的行继续按 material_id 展示。
  测试：有对齐方案时行数从 179→178，aligned 行使用标准名称和规格。

- [x] **OCR 识别结果字段纠错建议** ✓ 2026-05-29
  `apps/www/src/components/ExtractionEditor.vue`
  ExtractionEditor 新增 detectCorrection() 自动检测单价/合价混淆：
  当 unit_price ≈ total_price 且 qty > 1 时，显示红色 SwapOutlined 图标，
  提示”单价疑似为合价”并提供一键修正（点击自动计算正确单价）。

- [ ] **配电箱 BOM 元器件拆分导入**
  `apps/api/services/import_service.py`
  配电箱不按整箱导入，需解析箱内元器件行，每个元器件生成独立 `material + quote` 记录。
  匹配条件：元器件名称 + 规格 + 品牌系列。

- [x] **路由级权限守卫** ✓ 2026-05-29
  `main.py` 中 `include_router` 对非 auth 路由统一加 `dependencies=[Depends(get_current_user)]`。

### 前端

- [x] **比价流程增加”AI 对齐复核”确认界面** ✓ 2026-05-29
  `apps/www/src/views/compare/IndexView.vue`
  多供应商模式下新增 Step 2 “AI 对齐复核”：
  - 自动调用 LLM 分析报价对齐（后端 Mode 2 从 DB 查询报价行）
  - 展示对齐分组（Collapse + 确认/拒绝 Switch）、字段纠错建议（Table + 接受 Checkbox）
  - 确认后持久化到 bid_alignment_groups/items，再生成矩阵
  - 支持”跳过对齐”直接进入比价
  - 单供应商模式不触发对齐步骤

- [x] **OCR 识别结果：空价格字段高亮** ✓ 2026-05-29
  `unit_price` 为 null 时 a-input-number 已加 `:status="'error'"` 红色边框。

- [x] **OCR 识别结果：页面跳过告警** ✓ 2026-05-29
  IntakeUploader 已在 done 状态检查 `skipped_batches`，有值则弹 `message.warning`。

---

## P2 — 本期内完成

### 后端（API 补全）

- [x] **用户管理 API** ✓ 2026-05-29
  `apps/api/routes/users.py` + `apps/api/models/user.py`
  后端 CRUD + 状态切换 + 前端已接入真实 API。
  auth.py 改为数据库验证，首次启动自动种子管理员账号。

- [x] **操作日志 API** ✓ 2026-05-29
  `apps/api/routes/logs.py` + `apps/api/models/operation_log.py`
  后端列表 API + write_log 审计辅助函数。登录、用户管理操作已接入自动记录。
  前端已接入真实 API（含筛选、分页）。

- [x] **OCR 解析接入** ✓ 2026-05-29
  `apps/www/src/views/import/IndexView.vue`
  OCR tab 已接入真实 ExtractionPipeline：上传 PDF/图片 → intakeApi.upload(type=quote) →
  轮询 job 状态 → 解析结果填入 ExtractionEditor。品牌档位弹窗也已接入真实 API。
  测试：9 页桥架报价清单成功识别 199 行，供应商/品牌/单价/总价/税率均正确提取。

- [x] **比价报告 Excel 导出**（F6.4）✓ 已实现
  `apps/api/routes/export.py` 已实现 `GET /api/export/bid-matrix`，
  含横向对比矩阵、偏差百分比、推荐供应商标注，使用 openpyxl 带样式导出。

### 前端

- [x] **比价流程支持 Excel 上传** ✓ 2026-05-29
  `apps/www/src/views/compare/IndexView.vue` + `apps/api/routes/quotes.py`
  步骤二上传区域现已接受 .xlsx/.xls 文件，自动路由到 `POST /api/quotes/import`（直接解析，无需 OCR）。
  后端 ImportResult 新增 supplier_ids 字段；import 端点新增可选 project_id/supplier_id 表单参数。
  多供应商 Excel 自动拆分为多个 batch entry，每个 supplier 独立显示"Excel 已导入"标签。

- [x] **OCR 结果单价×数量校验** ✓ 2026-05-29
  ExtractionEditor 已在行尾显示 WarningOutlined 图标，tooltip 显示偏差百分比。

- [x] **气泡图品牌档位着色** ✓ 2026-05-29
  `get_dashboard_bubble()` 已按 (supplier, category) 找出主品牌，再从 brand_tiers 查档位填入 tier 字段。

---

## P3 — 后续迭代

- [ ] **物料编码对接**
  `apps/api/models/material.py` 中 `material_code` 字段目前自动生成临时编码。
  待一建提供自有编码体系后对接。

- [x] **数据流 / 待办队列**（F1.4）✓ 2026-05-29
  `apps/www/src/views/queue/IndexView.vue`
  增强版数据流页面：顶部统计卡片（全部/排队/识别中/完成/失败，点击快筛）、
  新增文件大小、识别条数、Token 用量列、刷新按钮。

- [x] **安装 PyJWT 启用真实 JWT** ✓ 2026-05-29
  pyjwt 已安装且已启用，auth.py 改为数据库验证 + JWT (HS256, 12h)。

- [x] **数据库迁移脚本** ✓ 2026-05-29
  `scripts/migrate_v1_to_v2.py` — 独立 Python 脚本（非 Alembic，保持简单）。
  支持 `--dry-run` 预览；幂等执行（检查列/表是否存在后再添加）。
  19 项变更：quotes/materials/suppliers 补列 + 创建 9 张新表。
  用法：`python scripts/migrate_v1_to_v2.py --db data/mempas.db`

- [x] **扫描件页面：多供应商批量识别进度** ✓ 2026-05-29
  `apps/www/src/views/compare/IndexView.vue` 批量模式
  多文件同时处理时，进度条仅显示单文件状态。可加总体进度指示（X/N 已完成）。

---

## 待一建确认

| # | 事项 | 状态 |
|---|------|------|
| 1 | 物料编码体系 | 一建编写中，系统先留白 |
| 2 | 各品类扩展属性匹配/差异划分 | 一建逐条比对中 |
| 3 | 品牌档位初稿 | 待一建提供 |
| 4 | 配电箱供应商数据补充（扫描版） | 后续补充 |
| 5 | 水箱数据补充（目标 ≥50 条） | 待补充 |
| 6 | 风机盘管供应商补充（目前仅 3 家） | 待补充 |
| 7 | 空调泵历史数据补充 | 待补充 |

---

## 已完成

- [x] **邀标建议 API 与前端主流程**
  `apps/api/routes/invite.py` + `apps/www/src/views/invite/IndexView.vue`
  已支持上传招标文件、识别清单、生成供应商推荐并保存邀标结果。

- [x] **比价 PDF 上传识别进度分阶段展示**
  `apps/api/intelligence/pipeline.py` + `apps/www/src/views/compare/IndexView.vue`
  已显示上传、接收、渲染 PDF、拆分页面、逐页识别、合并结果、整理结果、已识别等阶段和百分比。

- [x] **报价 PDF 逐页并发识别**
  `apps/api/intelligence/pipeline.py`
  已改为逐页识别，单批最多 10 页并发，提升多页扫描 PDF 处理效率。

- [x] **帮助中心匿名访问路由**
  `apps/www` help 路由
  已加入面向用户的比价、邀标端到端使用说明，并补充品牌档位确认说明。

- [x] **线上 nginx 上传限制调整**
  `apps/www/nginx.conf`
  上传限制已从 10M 调整为 100M，解决 15M/18M/36M PDF 上传 413 问题。
