# MEMPAS 招标比价系统 — 代码审核与 E2E 效率分析报告

**审核日期**: 2026-07-10  
**审核范围**: 全量代码审核 + E2E 流程效率加速分析  
**项目**: bid-compare (MEMPAS 机电材料查询比价分析系统)

---

## 一、E2E 流程全景与时间基线

### 1.1 完整流程（8 阶段）

```
招标 PDF/Excel 上传
  │
  ├─ Phase 1: 文档识别 (OCR + LLM)     ← 最重：30s~5min（3份并发）
  │    └─ 视觉分类 → OCR → 逐页LLM抽取 → 重试/切片 → 后处理
  │
  ├─ Phase 2: 报价暂存 (batch-confirm)  ← 轻量：~10s
  │
  ├─ Phase 3: 招标清单确认              ← 轻量：~2s（纯代码解析）
  │
  ├─ Phase 4: 对齐匹配                  ← 次重：16s(embedding) 或 3~6min(LLM填表)
  │    ├─ Path A: embedding 匹配 (~16s)
  │    └─ Path B: LLM 填表 (Wave1 + Wave2, ~3-6min)
  │
  ├─ Phase 5: 锚点复核 (人工)            ← 人工交互
  ├─ Phase 6: 锁定对齐快照               ← 轻量：<1s
  ├─ Phase 7: 比价矩阵生成               ← 轻量：~4s
  └─ Phase 8: 导出/审批                  ← 轻量：<1s
```

### 1.2 实测时间基线（来自 E2E 日志）

| 阶段 | 耗时 | 说明 |
|------|------|------|
| Phase 1 - OCR (3份PDF并发) | 30s ~ 5min | 绵存30s, 凯硕73s~5min, 泰科龙48s（受API限流影响波动大） |
| Phase 2 - 批量确认 | ~12s | 3份文档依次确认 |
| Phase 3 - 清单预览+确认 | ~2s | xlsx 解析，无 LLM |
| Phase 4a - embedding 匹配 | ~16s | 纯代码+embedding API |
| Phase 4b - LLM 填表 | 3~6min | Wave1(3家并发,Sem=3) + Wave2(锚点中心gap pass) |
| Phase 7 - 矩阵生成 | ~4s | 纯 DB 读取+计算 |
| **总耗时（自动部分）** | **~5~8min** | OCR + LLM填表 占 90%+ |

### 1.3 耗时分布

```
OCR 识别    ████████████████████████  ~35%
LLM 填表    ██████████████████████████████████  ~50%
其他        ████  ~15%
```

---

## 二、架构质量审核

### 2.1 优秀设计（值得保持）

| # | 设计 | 位置 | 评价 |
|---|------|------|------|
| 1 | **三层分离** | routes → services → intelligence | 路由只做参数解析+事务边界，业务逻辑全部在 services |
| 2 | **质量分级门禁** | AUTO/REVIEW/BLOCKED | 贯穿全链路，pending 数据不参与正式计算 |
| 3 | **两层数据模型** | ExtractionDraft → BidSubmission → Quote | 识别输出与正式报价隔离，防止污染历史数据 |
| 4 | **validate() 纯函数** | supplier_fill_llm.py:198-416 | LLM 只提议、代码裁决；anti-hallucination 不依赖模型行为 |
| 5 | **多 Key 轮转** | dashscope_ocr.py:339-367 | 每 Key Semaphore(6)，总并发 = 6×Key数 |
| 6 | **幂等 Job** | document_ingestion.py:44-67 | file_hash + type + context_hash 去重 |
| 7 | **SnapshotProvider** | snapshot_provider.py | record/replay 双模式，确保 replay 测试确定性 |
| 8 | **锚点向量复用** | analysis.py:1679 | 90 个锚点 embedding 只算一次，传给 N 个 supplier worker |
| 9 | **Tabular 旁路** | document_ingestion.py:204 | xlsx/csv 直接 pandas 解析，跳过 OCR+LLM |
| 10 | **Sequential 直连** | anchor_match.py:523-615 | 表格结构对齐时跳过 embedding，O(N) 直接匹配 |

### 2.2 架构风险

| # | 风险 | 位置 | 严重度 |
|---|------|------|--------|
| R1 | **analysis.py 过大** (108KB, 2471行, 29个端点) | routes/analysis.py | 中 — 应拆分 |
| R2 | **table_recognizer.py 过大** (81KB) | intelligence/table_recognizer.py | 中 — 应拆分 |
| R3 | **supplier_fill_llm.py 过大** (55KB) | services/supplier_fill_llm.py | 中 |
| R4 | **无生产级 LLM 缓存** | 全局 | 高 — 重复上传同 PDF 全量重跑 |
| R5 | **route 层混入业务逻辑** | analysis.py:1684-1855 (LLM fill 编排) | 中 — 应提取到 service |

---

## 三、E2E 效率瓶颈深度分析

### 瓶颈 1：含税字段重试 — 串行执行【P0 高影响】

**位置**: `table_recognizer.py:727-751`

**问题**: 当页面触发含税字段重试时（字段覆盖不足或税额恒等式失败），重试逐页**串行**执行。每次重试 = 完整的 `_process_page` = 可能 1~6 次 LLM 调用。

```python
# 当前代码（串行）
for _pno in sorted(_pages_needing_tax_retry):     # ← 逐页串行
    _data2, _raw2 = _process_page(_pno, ...)        # ← 可能 1-6 次 LLM 调用
```

**影响**: 如果 3 页需要重试，每页 2 次 LLM 调用，串行耗时 = 6 × 单次LLM延迟。

**优化方案**:
```python
# 改为并发
with ThreadPoolExecutor(max_workers=min(PAGE_CONCURRENCY, len(_pages_needing_tax_retry))) as exc:
    futs = {exc.submit(_process_page, _pno, ...): _pno for _pno in _pages_needing_tax_retry}
    for fut in as_completed(futs):
        _pno = futs[fut]
        _data2, _raw2, _metric2 = fut.result()
```

**预期收益**: 重试阶段耗时从 `N × 单页延迟` 降至 `ceil(N/并发度) × 单页延迟`，典型场景减少 50-70%。

---

### 瓶颈 2：自适应切片 — 串行执行【P0 高影响】

**位置**: `table_recognizer.py:1169-1179`

**问题**: 当页面抽取不足触发切片降级时，4 个独立切片**逐个串行**处理，每个需要 OCR + LLM 两次调用。

```python
# 当前代码（串行）
for tile in tiles:                                    # ← 4 个切片串行
    tile_html = _ocr_tile(provider, tile.image_bytes) # ← OCR 调用
    tile_data, tile_items = _llm_extract(...)          # ← LLM 调用
```

**影响**: 切片降级页耗时 = 4 × (OCR延迟 + LLM延迟) ≈ 4 × 15s = 60s+。

**优化方案**:
```python
# 改为并发
def _process_tile(tile):
    tile_html = _ocr_tile(provider, tile.image_bytes)
    llm_input, _, _, _ = _build_llm_input(tile_html, page_no)
    tile_data, tile_items = _llm_extract(provider, adapter.row_prompt, llm_input)
    for item in tile_items:
        item["_tile_bbox"] = list(tile.bbox_pct)
    return tile_items

with ThreadPoolExecutor(max_workers=min(PAGE_CONCURRENCY, len(tiles))) as exc:
    results = list(exc.map(_process_tile, tiles))
all_items = [item for sublist in results for item in sublist]
```

**预期收益**: 切片降级页耗时从 ~60s 降至 ~15s（单切片延迟），减少 75%。

---

### 瓶颈 3：LLM 填表并发上限过低 + 无限流保护【P0 高影响 + 安全风险】

**位置**: `analysis.py:1684` + `llm_provider.py:17-34` + `supplier_fill_llm.py:605-618`

**问题 1 — 并发上限固定且不可配**:
`asyncio.Semaphore(3)` 限制最多 3 个供应商并发填表。当供应商数 = 3 时无并行收益。

**问题 2 — LLM 填表路径完全缺乏限流保护（更严重）**:
与 OCR Provider（`dashscope_ocr.py`）有完善的 per-key Semaphore + 多 key 轮转 + 429 重试不同，LLM 填表路径：

| 保护机制 | OCR Provider | LLM Fill Path |
|---------|-------------|---------------|
| Per-key Semaphore | ✅ `Semaphore(6)` × 4 keys = 24 | ❌ 无 |
| 多 Key 轮转 | ✅ `itertools.cycle(keys)` | ❌ 只用第一个 key |
| 429 重试 | ✅ 线性退避 5 次 | ❌ 无任何重试 |
| 超时控制 | ✅ 90s | ⚠️ 300s（太长，无流式反馈） |

`llm_provider.py` 的 `get_dashscope_client()` 只取 `multi.split(",")[0]`（第一个 key），
`supplier_fill_llm.py:605` 的 `call_llm()` 直接调用 `client.chat.completions.create()`，
**没有 try/except 处理 `RateLimitError`**。

**429 证据**: E2E 日志显示凯硕 OCR 耗时从 73s（e2e_align.log）暴增到 308s（e2e_anchor_clean.log），
4.2 倍波动高度暗示 429 限流 + 退避重试。两个路径共享同一 DashScope 账号配额。

**安全优化方案**（必须先做这些，再提升并发）:

```python
# 步骤 1: llm_provider.py — 多 key 轮转客户端
def get_dashscope_clients() -> list["OpenAI"]:
    """返回所有配置 key 的 client 列表，支持轮转。"""
    s = get_settings()
    keys_env = getattr(s, "DASHSCOPE_API_KEYS", "") or ""
    keys = [k.strip() for k in keys_env.split(",") if k.strip()]
    if not keys:
        single = s.DASHSCOPE_API_KEY or ""
        keys = [single] if single else []
    return [OpenAI(api_key=k, base_url=s.DASHSCOPE_BASE_URL) for k in keys]

# 步骤 2: supplier_fill_llm.py — call_llm 加 429 重试
_FILL_PER_KEY_CONCURRENCY = max(1, int(os.getenv("LLM_FILL_PER_KEY_CONCURRENCY", "3")))
_fill_key_cycle = None  # itertools.cycle of client list
_fill_key_sems = {}     # per-key semaphore

def call_llm(prompt, clients, model, timeout=300):
    for attempt in range(_MAX_RETRIES):
        client = _next_fill_client()
        sem = _fill_key_sems[_key_for(client)]
        sem.acquire()
        try:
            resp = client.chat.completions.create(...)
        except openai.RateLimitError:
            sem.release()
            wait = min(2 ** attempt + random.uniform(0, 1), 30)  # 指数退避+抖动
            time.sleep(wait)
            continue
        sem.release()
        return _parse_llm_json(resp.choices[0].message.content), resp.usage.total_tokens

# 步骤 3: analysis.py — 并发可配（在步骤 1-2 完成后）
_fill_concurrency = max(3, int(os.getenv("LLM_FILL_CONCURRENCY", "5")))
sem = asyncio.Semaphore(_fill_concurrency)
```

---

### 瓶颈 4：Wave2 重复嵌入锚点向量【P1 中影响】

**位置**: `analysis.py:1741` (`anchor_vecs=None`)

**问题**: Wave2 锚点中心 pass 传入 `anchor_vecs=None`，导致 `fill_one_supplier_anchor_centric` 重新嵌入 gap 锚点子集。但 gap 锚点是全量锚点的子集，可以直接切片预计算向量。

```python
# 当前：Wave2 重新嵌入
lambda _sid=sid, _al=_aligned_non_suspect: fill_one_supplier_anchor_centric(
    rows_by_sid[_sid], gap_anchor_views, client,
    anchor_vecs=None,  # ← 重新嵌入
    ...
)
```

**优化方案**:
```python
# 预计算 gap 子集向量（一次，复用给所有 supplier）
gap_seq_set = set(gap_seqs)
gap_vec_map = {av.seq: anchor_vecs[i] for i, av in enumerate(anchor_views) if av.seq in gap_seq_set}
gap_anchor_vecs = [gap_vec_map[av.seq] for av in gap_anchor_views]

# 传给所有 supplier worker
lambda _sid=sid, _al=_aligned_non_suspect: fill_one_supplier_anchor_centric(
    rows_by_sid[_sid], gap_anchor_views, client,
    anchor_vecs=gap_anchor_vecs,  # ← 复用预计算
    ...
)
```

**预期收益**: 消除 N 家 × 1 次 embedding 调用（~20 次 API 调用），节省 ~10-15s。

---

### 瓶颈 5：链式方向纠正 — 串行 re-OCR【P1 中影响】

**位置**: `table_recognizer.py:515-549`

**问题**: 方向纠正对非 sample 页逐页串行 re-OCR。每个链的探测虽然用了 sample 页，但非 sample 页的修正 OCR 是逐页串行的。

```python
for _p in _chain:                    # ← 逐页串行
    if _p not in _probe_cache:
        _res, _ = provider.ocr_pages_with_roles([_rot_img])  # ← 单页 OCR
```

**优化方案**: 将同一链内非 sample 页的 re-OCR 批量提交：

```python
# 收集需要 re-OCR 的页面
to_re_ocr = [(_p, _rotate_png_bytes(page_imgs[_p], _angle)) 
             for _p in _chain if _p not in _probe_cache]
if to_re_ocr:
    pages, imgs = zip(*to_re_ocr)
    results, _ = provider.ocr_pages_with_roles(list(imgs))  # 批量并发
    for _p, (_cls, _html) in zip(pages, results):
        page_htmls[_p - 1] = _html
        page_imgs[_p] = ...
```

**预期收益**: 链内 N 页 re-OCR 从 N × 单页延迟降至 1 × 批量延迟。

---

### 瓶颈 6：无生产级 LLM/OCR 缓存【P1 中影响】

**位置**: 全局（仅 SnapshotProvider 用于测试）

**问题**: 生产环境中，同一 PDF 重新上传会全量重跑 OCR + LLM。用户反复调试同一文档时浪费大量 API 调用和时间。

**优化方案**: 将 SnapshotProvider 的 record 模式引入生产，以 `sha256(image_bytes)` 为 key 做磁盘缓存：

```python
class ProductionCacheProvider:
    """Wraps real provider with optional disk cache."""
    def __init__(self, real_provider, cache_dir, enabled=True):
        self._real = real_provider
        self._cache_dir = cache_dir
        self._enabled = enabled

    def ocr_pages_with_roles(self, images):
        if not self._enabled:
            return self._real.ocr_pages_with_roles(images)
        results = []
        misses = []
        for i, img in enumerate(images):
            key = sha256(img).hexdigest()
            cached = self._load_cache(f"ocr_{key}")
            if cached:
                results.append(cached)
            else:
                misses.append((i, img, key))
        if misses:
            miss_imgs = [m[1] for m in misses]
            miss_results, _ = self._real.ocr_pages_with_roles(miss_imgs)
            for (idx, _, key), res in zip(misses, miss_results):
                self._save_cache(f"ocr_{key}", res)
                # ... merge back
```

**预期收益**: 重复上传同一 PDF 时，OCR 阶段从 30s~5min 降至 <1s。

---

### 瓶颈 7：线性退避策略【P2 低影响】

**位置**: `dashscope_ocr.py:41`

**问题**: 重试使用线性退避（3, 6, 9, 12, 15s），而非指数退避。在 429 限流场景下，线性退避恢复速度慢。

```python
_RETRY_DELAY = 3  # linear: 3, 6, 9, 12, 15
```

**优化方案**:
```python
_RETRY_BASE = 2   # exponential: 2, 4, 8, 16, 32 (with jitter)
_RETRY_MAX = 30

def _backoff_delay(attempt):
    import random
    delay = min(_RETRY_BASE * (2 ** attempt), _RETRY_MAX)
    return delay + random.uniform(0, 1)  # jitter
```

---

### 瓶颈 8：N+1 数据库查询【P2 低影响】

**位置**: `bid_matrix.py:52` (`_get_item_data`)

**问题**: 矩阵构建时每个 cell 单独 `db.get()`，90 锚点 × 3 供应商 = 270 次查询。

**优化方案**: 批量预加载：
```python
all_items = db.query(BidAlignmentItem).filter(
    BidAlignmentItem.group_id.in_(group_ids)
).all()
items_by_key = {(i.anchor_seq, i.supplier_id): i for i in all_items}
```

---

### 瓶颈 9：纯 Python 余弦矩阵【P2 低影响】

**位置**: `anchor_match.py:108`

**问题**: 余弦相似度用纯 Python 计算（~200×90 scale），但 numpy 已在依赖中。

**优化方案**:
```python
import numpy as np

def _cosine_matrix_np(quote_vecs, anchor_vecs):
    q = np.array(quote_vecs)  # [Q, D]
    a = np.array(anchor_vecs) # [A, D]
    q_norm = q / np.linalg.norm(q, axis=1, keepdims=True)
    a_norm = a / np.linalg.norm(a, axis=1, keepdims=True)
    return q_norm @ a_norm.T  # [Q, A]
```

---

## 四、优化优先级矩阵

| 优化项 | 影响等级 | 实现难度 | 预期耗时减少 | 建议优先级 |
|--------|---------|---------|-------------|-----------|
| 含税重试并发化 | P0 | 低 | 50-70% 重试耗时 | ★★★★★ |
| 切片降级并发化 | P0 | 低 | 75% 切片页耗时 | ★★★★★ |
| LLM填表并发可配 | P0 | 极低 | 30-50% (6家时) | ★★★★★ |
| Wave2 向量复用 | P1 | 低 | ~10-15s | ★★★★☆ |
| 链式方向批量OCR | P1 | 中 | 链内 N→1 批次 | ★★★★☆ |
| 生产级OCR缓存 | P1 | 中 | 重复上传→<1s | ★★★★☆ |
| 指数退避+抖动 | P2 | 极低 | 429恢复更快 | ★★★☆☆ |
| N+1查询批量化 | P2 | 低 | ~1-2s | ★★★☆☆ |
| numpy余弦矩阵 | P2 | 低 | <1s | ★★☆☆☆ |

---

## 五、综合效率提升预估

### 5.1 当前 E2E 耗时分解（3 家供应商，90 锚点）

```
Phase 1 OCR:     73s  (3 PDF 并发, 最长文档决定)
Phase 2 确认:    12s
Phase 3 清单:     2s
Phase 4 匹配:    16s  (embedding) 或 180-360s (LLM填表)
Phase 7 矩阵:     4s
────────────────────
总计:           ~107s (embedding路径) 或 ~271-451s (LLM填表路径)
```

### 5.2 优化后预估

```
Phase 1 OCR:     45-55s  (切片+重试并发化, 减少 ~25%)
Phase 2 确认:    12s
Phase 3 清单:     2s
Phase 4 匹配:    90-180s (LLM填表并发提升+Wave2向量复用)
Phase 7 矩阵:     3s  (N+1修复)
────────────────────
总计:           ~152-252s (LLM填表路径)
提升:           ~30-45%
```

### 5.3 极端优化场景（含生产缓存）

如果用户重复上传同一 PDF（调试场景）：
```
Phase 1 OCR:     <1s  (缓存命中)
Phase 4 匹配:    90-180s (LLM结果也可缓存)
────────────────────
总计:           <2min (首次) / <10s (缓存命中)
```

---

## 六、代码质量发现

### 6.1 代码组织

- **routes/analysis.py** (108KB) 建议拆分为：`tender_routes.py`、`alignment_routes.py`、`matrix_routes.py`、`analytics_routes.py`
- **table_recognizer.py** (81KB) 建议拆分为：`recognizer_core.py`、`recognizer_retry.py`、`recognizer_tiling.py`、`recognizer_orientation.py`
- **supplier_fill_llm.py** (55KB) 建议拆分为：`fill_core.py`、`fill_validator.py`、`fill_prompt.py`、`fill_anchor_centric.py`

### 6.2 潜在问题

| # | 问题 | 位置 | 风险 |
|---|------|------|------|
| 1 | `_PER_KEY_CONCURRENCY` 和 `PAGE_CONCURRENCY` 两套并发参数可能冲突 | dashscope_ocr.py:45 + pipeline.py:57 | 实际并发 = min(PAGE_CONCURRENCY, _PER_KEY_CONCURRENCY × keys)，需文档化关系 |
| 2 | `extract_supplier_name_from_cover` 在聚合后串行触发 | pipeline.py:281 | 可在 OCR 阶段并行预提取 |
| 3 | enhance.py 限制 60 items/prompt | enhance.py:89 | 大文档后半部分只获得启发式分类 |
| 4 | 300s 单次 LLM 超时 | supplier_fill_llm.py:611 | 无流式反馈，用户长时间等待无进度 |
| 5 | sqlite3 直连在 E2E 脚本中绕过 ORM | test_e2e_anchor.py:42 | 生产应禁用，仅限测试 |

### 6.3 测试覆盖

- ✅ 单元测试：anchor_match, supplier_fill validator, table_parser, canonical
- ✅ Replay 测试：SnapshotProvider 确保确定性
- ✅ Fresh E2E：真实 API 链路验证
- ⚠️ 缺少：并发安全性测试（多 Job 同时写入同一 project）
- ⚠️ 缺少：超时/重试路径的单元覆盖

---

## 七、行动建议

### 立即可做（1-2 天）
1. **切片降级并发化** — 改 `_try_tiled_extraction` 为 ThreadPoolExecutor
2. **含税重试并发化** — 改 tax-field retry 为 ThreadPoolExecutor
3. **LLM 填表并发可配** — `asyncio.Semaphore(int(os.getenv("LLM_FILL_CONCURRENCY", "5")))`
4. **指数退避+抖动** — 改 `dashscope_ocr.py` 重试策略

### 短期（1 周）
5. **Wave2 向量复用** — 切片预计算 anchor_vecs 传入
6. **链式方向批量 OCR** — 同链非 sample 页批量提交
7. **N+1 查询批量化** — bid_matrix 预加载 alignment items

### 中期（2-4 周）
8. **生产级 OCR/LLM 缓存** — 引入 content-hash 磁盘缓存
9. **大文件拆分** — analysis.py / table_recognizer.py / supplier_fill_llm.py
10. **enhance.py 分批** — 60 items 限制改为分批处理

---

## 八、实现结果与实测数据（2026-07-10 更新）

### 8.1 已实现优化（分支 `perf/e2e-optimization`）

| 提交 | 优化项 | 文件 | 说明 |
|------|--------|------|------|
| `7faa340` | 含税重试并发化 | `table_recognizer.py` | 串行 for → ThreadPoolExecutor |
| `7faa340` | 切片降级并发化 | `table_recognizer.py` | 4 tiles 串行 → 并发 |
| `7faa340` | 链式方向批量 OCR | `table_recognizer.py` | 逐页 re-OCR → 单次批量调用 |
| `7faa340` | 指数退避+抖动 | `dashscope_ocr.py` | 线性(3,6,9,12,15s) → 指数(2,4,8,16,32)+jitter |
| `2ba84fc` | PyMuPDF 评估（未采用） | `document_loader.py` | 单线程快约 20%，但商业使用受 AGPL 约束，运行时保持 pypdfium2 |
| `2ba84fc` | embedding v3→v4 | `anchor_match.py` | 模型升级 |
| `f39f9ba` | 跳过 recall 页 tiling | `table_recognizer.py` | recall 页 best-effort，省 8 次 API 调用 |
| `f39f9ba` | PDF 文本预筛分类 | `table_recognizer.py` | PyMuPDF get_text() 零成本排除非表格页 |
| `f39f9ba` | 跳过无表格页 LLM | `table_recognizer.py` | table_count=0 + row_count=0 → 不调 LLM |

### 8.2 未实现优化及原因

| 优化项 | 原因 |
|--------|------|
| LLM 填表并发提升 | LLM 填表路径(call_llm)无限流保护，提升并发会触发 429 |
| 批量 LLM 抽取 | 瓶颈是单次调用延迟(20s)不是调用次数；合并页使每次调用更慢 |
| 跳过方向探测 | 仅省 4s 但有漏检旋转页风险 |
| PDF 渲染 ThreadPoolExecutor | pypdfium2 不线程安全（官方文档确认会 crash） |
| PyMuPDF ThreadPoolExecutor | PyMuPDF 也不线程安全（官方 FAQ 确认） |

### 8.3 单文档 API 调用耗时拆解（上海绵存，31 页，93.5s）

```
  缩略图渲染(31页,PyMuPDF)   ████████████████      ~15s  16%
  视觉分类(qwen3-vl-flash)    ██████████████████████ ~21s  22%
  全分辨率渲染(6页,PyMuPDF)   ████████              ~8s   9%
  OCR(qwen-vl-ocr-latest)    █████████████████████████  ~25s  27%  ← 最大瓶颈
  方向探测                    ████               ~4s   4%
  LLM抽取(qwen3.6-flash)     ████████████████████  ~20s  21%
  切片(已跳过)                0s                  ← 优化生效
  后处理                      █                   ~0.5s  1%
                             ─────────────────
  总计                        93.5s
```

按模型拆解：
- **qwen-vl-ocr-latest**: 7 次调用, 29s (31%) — OCR table_parsing
- **qwen3-vl-flash**: 4 批, 21s (22%) — 视觉页面分类
- **qwen3.6-flash**: 7 次, 20s (21%) — 结构化 JSON 抽取
- **PyMuPDF (CPU)**: 37 次, 23s (25%) — PDF 渲染

### 8.4 E2E 测试结果（3 PDF 并发，同条件横向对比）

| 运行 | 配置 | 绵存 | 凯硕 | 泰科龙 | Wall-clock | 行数 |
|------|------|------|------|--------|-----------|------|
| Run 2 | pypdfium2 基线 | 123s | 129s | 209s | **209s** | 89 |
| Run 3 | +提额 | 114s | 127s | 200s | **200s** | 89 |
| Run 4 | qwen3-vl-flash 模型 | 114s | 126s | 222s | **222s** | 89 |
| Run 5 | qwen-vl-ocr 模型 | 124s | 139s | 233s | **233s** | 89 |
| Run 7 | PyMuPDF 渲染 | 116s | 125s | 193s | **193s** | 89 |
| **Run 8** | **PyMuPDF + 全部优化** | **116s** | **125s** | **179s** | **179s** | **89** |

**最终提升**: E2E wall-clock 209s → 179s (**-14%**), 单文档 123s → 93.5s (**-24%**)
**准确性**: 89 行, 100% 匹配率, 0 冲突 — 全程不变

### 8.5 DashScope 限流关键发现

- **限流按主账号维度计算** — 4 个 API Key 共享同一个 RPM/TPM 配额
- 多 Key 轮转不能提高总吞吐量，只能防 RPS 爆发
- 已申请临时限流提额：qwen3.6-flash 20M TPM, qwen-plus 10M TPM, qwen3-vl-flash 10M TPM
- 效果约 4%，不显著（OCR 模型 qwen-vl-ocr-latest 未找到提额入口）

### 8.6 pypdfium2 / PyMuPDF 线程安全结论

| 库 | 线程安全 | 官方建议 |
|----|---------|---------|
| pypdfium2 | ❌ 不安全，跨线程会 crash | 用 ProcessPoolExecutor |
| PyMuPDF | ❌ 不安全 | 用 multiprocessing |

两者都不能用 ThreadPoolExecutor 并行渲染。PyMuPDF 单线程约快 20%，但因 AGPL-3.0 商业许可限制，本项目不采用；运行时与文本预筛统一使用 pypdfium2。

### 8.7 剩余可优化项

| 优化 | 预期收益 | 难度 | 说明 |
|------|---------|------|------|
| LLM 填表限流基础设施 | 30-50% | 中 | 需先给 call_llm 加 per-key sem + 429 重试 |
| 生产级 OCR 缓存 | 重复上传→<1s | 中 | content-hash 磁盘缓存 |
| qwen-vl-ocr-latest 限流提额 | 10-20% | 低 | 百炼控制台申请 |
| ProcessPoolExecutor 渲染 | ~15s | 高 | 进程间 PNG 传递开销可能抵消收益 |

---

## 附录：关键文件索引

| 文件 | 大小 | 核心职责 |
|------|------|---------|
| `apps/api/routes/analysis.py` | 108KB | 29 个 API 端点，E2E 编排 |
| `apps/api/intelligence/table_recognizer.py` | 81KB | OCR+LLM 识别骨架 |
| `apps/api/services/supplier_fill_llm.py` | 55KB | LLM 供应商填表代理 |
| `apps/api/services/anchor_match.py` | 43KB | Embedding 锚点匹配 |
| `apps/api/services/bid_matrix.py` | 43KB | 比价矩阵构建 |
| `apps/api/intelligence/pipeline.py` | 28KB | 识别管线编排 |
| `apps/api/intelligence/providers/dashscope_ocr.py` | 28KB | DashScope API 封装 |
| `apps/api/intelligence/document_loader.py` | 6KB | PDF 渲染（已切换 PyMuPDF） |
| `apps/api/services/document_ingestion.py` | 14KB | 文档摄入服务 |
