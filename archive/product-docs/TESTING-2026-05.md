# MEMPAS 测试文档

> 覆盖范围：后端单元测试、集成测试、端到端 LLM 测试；前端组件/API/路由测试。
> 文档随代码同步维护，每次 Phase 完成后更新。

---

## 一、测试分层策略

| 层级 | 工具 | 运行条件 | 目的 |
|------|------|----------|------|
| **单元测试** | pytest + 内存 SQLite | 任何时候（无外部依赖） | 单个函数/类逻辑，边界值 |
| **集成测试** | pytest + 临时 SQLite + MockProvider | 任何时候（无外部依赖） | 多组件联动，完整业务流程，不消耗 LLM 配额 |
| **端到端测试（e2e）** | pytest + 真实 Qwen-VL API | 手动触发，需要 `DASHSCOPE_API_KEY` | 验证 LLM 精度与 API 连通性 |
| **前端单元测试** | vitest + happy-dom | 任何时候 | 组件渲染、API 契约、路由、状态管理 |

### 默认运行（CI）

```bash
# 后端：跳过 e2e，运行所有单元+集成测试
cd apps/api
pytest -m "not e2e" -v

# 前端：所有单元测试
cd apps/www
npm run test:unit
```

### 手动触发 e2e

```bash
cd apps/api
DASHSCOPE_API_KEY=sk-xxxxx pytest -m e2e -v
```

---

## 二、后端测试文件清单

### 2.1 `test_qwen_vl_provider.py` — 16 个测试

**覆盖**：智能引擎层 (`apps/api/intelligence/providers/`)

| 测试类 | 用例数 | 测试内容 |
|--------|--------|---------|
| `TestParseJsonStrict` | 8 | JSON 容错解析：markdown ``` 围栏、前缀说明文字、嵌套 JSON、trailing comma（对象/数组/组合） |
| `TestCandidateBuilder` | 4 | 模型候选列表：ENV 指定优先、fallback 顺序、去重、空 ENV |
| `TestMockProvider` | 4 | MockProvider 三种模式：canned/fixture/minimal-from-schema |

**关键断言**：`_parse_json_strict` 能正确处理 Qwen-VL 返回的各种非标准 JSON 格式。

---

### 2.2 `test_document_ingestion.py` — 9 个测试

**覆盖**：`apps/api/services/document_ingestion.py`

| 测试类 | 用例数 | 测试内容 |
|--------|--------|---------|
| `TestCreateJob` | 6 | Job 创建、SHA256+context 幂等去重（同文件不同供应商得到不同 job）、上下文缺失校验 |
| `TestRunJob` | 2 | Job 状态机：PENDING → RUNNING → DONE/FAILED；异常时 rollback |
| `TestStuckJobRecovery` | 1 | `recover_stuck_jobs()` 把 RUNNING 超时的 job 标为 FAILED |

---

### 2.3 `test_intake_routes.py` — 7 个测试

**覆盖**：`apps/api/routes/intake.py`（HTTP 接口层）

| 测试类 | 用例数 | 测试内容 |
|--------|--------|---------|
| `TestUpload` | 4 | 上传文件返回 job_id、type 参数校验、supplier_id/project_id 上下文注入 |
| `TestJobLifecycle` | 3 | 轮询 job 状态、job 不存在返回 404、job 列表过滤 |

**运行配置**：`conftest.py` 设置 `EXTRACTION_MODE=inline`，使 TestClient 同步等待 job 完成，无需轮询。

---

### 2.4 `test_invite_integration.py` — 8 个测试

**覆盖**：邀标主线端到端（intake → 推荐 → 保存）

| 测试类 | 用例数 | 测试内容 |
|--------|--------|---------|
| `TestPhase2InviteFlow` | 4 | 上传招标文件→DONE、推荐结果 TOP-1 在历史成交前 3 内、保存写 DB、幂等保存不重复 |
| `TestInferCategories` | 4 | 品类推断：明确品类、关键词匹配、去重、无法识别时返回空 |

**测试数据**：使用 `docs/data/桥架报价单格式模板_汇总.csv`（前 120 行）和 `docs/data/阀门询价格式_汇总.csv`（前 80 行）播种测试数据库。

---

### 2.5 `test_compare_integration.py` — 3 个测试

**覆盖**：比价主线端到端（intake → batch-confirm → bid-matrix）

| 测试类 | 用例数 | 测试内容 |
|--------|--------|---------|
| `TestPhase3CompareFlow` | 3 | 完整流水线（两家供应商各上传→confirm→bid-matrix）、quote job 类型校验、unknown brand 上报 |

**测试数据**：MockProvider 返回结构化报价数据，测试 DB 不依赖真实 LLM 调用。

---

### 2.6 `test_audit_fixes.py` — 41 个回归测试

审计后修复的所有 bug 的回归覆盖，防止二次引入。

| 测试类 | 用例数 | 对应修复 |
|--------|--------|---------|
| `TestContextAwareIdempotency` | 5 | C1：同文件不同供应商得到独立 job（`_hash_context` 按业务上下文区分） |
| `TestBatchConfirmIdempotency` | 1 | C2：双击 batch-confirm 不创建重复 Quote 行 |
| `TestBatchConfirmShapeGuard` | 3 | M5：items 为非 list 或含非 dict 元素时安全处理而非崩溃 |
| `TestCategoryTokenMatch` | 5 | H1：词边界检测，"止回阀门"不误匹配"阀门" |
| `TestInviteSaveValidation` | 1 | H5：全部 supplier_ids 无效时 400 并回滚，不留孤儿 TenderDocument |
| `TestQwenVLDynamicTimeout` | 1 | H7：timeout 随页数动态扩展（BASE + PER_PAGE × pages） |
| `TestJsonParseTrailingComma` | 3 | H8：JSON 末尾逗号（对象/数组/组合）在 `}` `]` 前自动清除 |
| `TestOrphanProjectGuard` | 2 | batch-confirm 传 project_id 不存在时返回 404 而非 500 |
| `TestPeriodicStuckJobSweep` | 1 | L4：定时协程能在测试环境中正确恢复 RUNNING 超时 job |
| `TestSupplierDeletionGuard` | 4 | 业务决策：有报价/邀标记录的供应商拒绝删除（409）；未引用的可删除 |
| `TestStandardizeStability` | 9 | 标准化函数：全角→半角、大小写、空白、零宽字符、幂等、同义词、DN/尺寸 |
| `TestConcurrentInviteSave` | 1 | L3：并发重复 save 请求时 IntegrityError 安全处理（无 500） |
| `TestThreadPoolExtraction` | 4 | L4：线程池统计、健康端点 `/api/health/queue`、inline 模式同步执行 |
| `TestQwenVLBadModelMemo` | 1 | H6：BadRequestError 的模型加入 `_known_bad` 集合，后续调用跳过 |

---

### 2.7 `test_e2e_qwen_vl.py` — 5 个 e2e 测试

**标记**：`@pytest.mark.e2e`；默认 CI 不运行。

**前置条件**：
- `DASHSCOPE_API_KEY` 已设置（真实密钥）
- `DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1`
- 网络可达阿里云百炼 API

| 测试类 | 用例 | 测试内容 |
|--------|------|---------|
| `TestConnectivity` | `test_api_reachable` | DashScope 兼容端点连通；`client.models.list()` 返回 ≥ 1 个模型 |
| `TestSyntheticExtraction` | `test_synthetic_quote_extraction` | 合成 PNG（3 行报价表）→ Qwen-VL 提取 → 字段命中率 100% |
| `TestSyntheticExtraction` | `test_pipeline_postprocess` | ExtractionPipeline 后处理（DN 规范、同义词、单位）正确应用 |
| `TestRealDataAccuracy` | `test_real_quote_field_accuracy` | **真实数据精度验证**：20/20 字段 = 100%（见下方详情） |
| `TestRealDataAccuracy` | `test_real_tender_extraction` | **真实数据召回率**：5/5 材料行 = 100% |

#### 真实数据精度详情

**报价单测试**（`real_quote.png`）：
- **数据来源**：`docs/data/阀门询价格式_汇总.csv` 前 4 行，由 `generate_test_docs.py` 渲染为 PNG
- **真值文件**：`apps/api/tests/fixtures/real_quote_truth.json`
- **精度结果**：4 物料 × 5 字段（material + spec + brand + qty + unit_price）= **20/20 = 100.0%** ✨
- **实测耗时**：~11-14 秒/文档（4-5 行表格）
- **token 用量**：~1500 tokens/请求

**招标文件测试**（`real_tender.png`）：
- **数据来源**：`docs/data/桥架报价单格式模板_汇总.csv` 前 5 行，渲染为 PNG
- **真值文件**：`apps/api/tests/fixtures/real_tender_truth.json`
- **召回率**：5/5 材料行 = **100.0%** ✨

**实际响应落盘**（供 MockProvider 回归使用）：
- `apps/api/tests/fixtures/live_real_quote_result.json`
- `apps/api/tests/fixtures/live_real_tender_result.json`
- `apps/api/tests/fixtures/live_synthetic_quote.json`

---

## 三、前端测试文件清单

| 文件 | 用例数 | 测试内容 |
|------|--------|---------|
| `api.test.ts` | 22 | API 客户端契约：`intakeApi` / `inviteApi` / `quoteApi` / `analysisApi` 请求格式、响应类型映射、FormData（无显式 Content-Type，axios 自动设置 multipart boundary） |
| `router.test.ts` | 4 | 路由守卫、页面跳转、权限重定向 |
| `store.test.ts` | 3 | Pinia store（全局状态、reset） |
| `extraction-guards.test.ts` | 10 | 运行时 shape 守卫：`asTenderShape()` / `asQuoteShape()` / `asExtractionShape()` 强制类型转换与 console.warn |

---

## 四、测试数据

### 4.1 使用的原始资料文件

| 文件 | 用于 | 取行数 |
|------|------|--------|
| `docs/data/阀门询价格式_汇总.csv` | e2e 报价单精度测试、邀标集成测试播种 | 前 4 行（精度）/ 前 80 行（播种） |
| `docs/data/桥架报价单格式模板_汇总.csv` | e2e 招标文件召回测试、集成测试播种 | 前 5 行（精度）/ 前 120 行（播种） |

> 以上两个文件均为 UTF-8 BOM 编码（Excel 导出格式），代码读取时使用 `encoding='utf-8-sig'`。

### 4.2 生成测试 Fixture

```bash
# 从 docs/data/ 真实 CSV 生成 PNG 图片和真值 JSON
cd apps/api
python tests/fixtures/generate_test_docs.py
# 输出：
#   tests/fixtures/real_quote.png        ← 阀门询价格式渲染图
#   tests/fixtures/real_quote_truth.json ← 人工标注真值（4 行×5 字段）
#   tests/fixtures/real_tender.png       ← 桥架报价单渲染图
#   tests/fixtures/real_tender_truth.json ← 人工标注真值（5 行材料）
```

### 4.3 MockProvider Fixture

| 文件 | 说明 |
|------|------|
| `tests/fixtures/quote.json` | mock 报价单提取结果（canned 模式） |
| `tests/fixtures/tender.json` | mock 招标文件提取结果（canned 模式） |
| `tests/fixtures/live_*.json` | 真实 Qwen-VL 返回落盘，供回归对比 |

---

## 五、测试环境配置

### `apps/api/tests/conftest.py` 关键设置

```python
# 使所有测试同步执行 LLM 提取（不走线程池），避免 TestClient 时序问题
os.environ.setdefault("EXTRACTION_MODE", "inline")
```

### 环境变量

| 变量 | 单元/集成测试 | e2e 测试 |
|------|-------------|---------|
| `EXTRACTION_MODE` | `inline`（conftest 自动设置） | `thread`（默认） |
| `LLM_PROVIDER` | 自动 `mock`（无 API key） | `qwen_vl` |
| `DASHSCOPE_API_KEY` | 不需要 | **必须** |
| `DASHSCOPE_BASE_URL` | 不需要 | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `LLM_VISION_MODEL` | 不需要 | `qwen3-vl-plus`（可选，有默认探活） |

---

## 六、运行命令

```bash
# ── 后端 ──────────────────────────────────────────────
cd apps/api

# 全量运行（跳过 e2e，~5 秒）
pytest -m "not e2e" -v

# 指定测试文件
pytest tests/test_audit_fixes.py -v

# 指定测试类/用例
pytest tests/test_audit_fixes.py::TestCategoryTokenMatch -v
pytest tests/test_audit_fixes.py::TestStandardizeStability::test_fullwidth_to_halfwidth -v

# 覆盖率报告
pytest -m "not e2e" --cov=apps/api --cov-report=term-missing

# e2e（需要 API key）
DASHSCOPE_API_KEY=sk-xxxxx pytest -m e2e -v -s

# ── 前端 ──────────────────────────────────────────────
cd apps/www

# 全量运行
npm run test:unit

# watch 模式
npm run test:unit -- --watch

# 覆盖率
npm run test:unit -- --coverage
```

---

## 七、测试覆盖目标

| 模块 | 目标 | 当前状态 |
|------|------|---------|
| `services/` | ≥ 80% 行覆盖 | 达到（document_ingestion, supplier_recommend, standardize 均有完整覆盖） |
| `intelligence/` | ≥ 80% 行覆盖 | 达到（providers, pipeline, pipeline 后处理） |
| `routes/` | 主路径 + 错误路径 | 达到（HTTP 层 7 + 比价集成 3 + 邀标集成 8） |
| LLM 精度（报价单） | ≥ 95% 字段命中率 | **100%**（20/20 字段，`阀门询价格式_汇总.csv`） |
| LLM 精度（招标文件） | ≥ 90% 材料召回率 | **100%**（5/5 材料行，`桥架报价单格式模板_汇总.csv`） |

---

## 八、已知限制与待完善项

- **Playwright E2E 未实施**：前端仅有 vitest 单元测试，无浏览器级别 click-through 测试（计划 Phase 4）
- **Excel 上传测试**：当前 e2e 仅测 PNG/图片输入，未测真实 PDF 上传（待用户提供真实招标 PDF）
- **并发 LLM 测试**：`TestConcurrentInviteSave` 使用 `LyingFirst` mock 模拟竞态，非真实并发（真实并发需 PostgreSQL + 多进程）
- **CSV 编码说明**：`docs/data/` 下所有 CSV 为 `UTF-8 with BOM`（Excel 导出），读取时须用 `encoding='utf-8-sig'`；`docs/design/` 下所有 Markdown 为 `UTF-8 no BOM`（AI/git 友好）
