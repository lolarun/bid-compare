# Alembic 版本化迁移引入（P2-1 第一步）

> 设计日期：2026-06-22
> 范围：把 schema 演进从 `create_all` + `_ensure_sqlite_schema` 的临时机制，迁到版本化 Alembic migration。
> 依据：`docs/design/12` P2-1 / 11.2；CLAUDE.md §8；`.claude/rules/database-safety.md`。
> 触发原因：用户选定「先做 P2-1 Alembic，再让 P1-3/P1-4 走迁移」。

## 1. 现状（已 file:line 核查）

| 事实 | 位置 |
|------|------|
| 生产库 = `data/mempas.db`（21MB，`data/` 已 gitignore，目录内已有多份 `.bak`） | `core/database.py:8-12` |
| 启动建表 = `create_all()` + `_ensure_sqlite_schema()` | `core/database.py:42-45`，`main.py:113` |
| `_ensure_sqlite_schema` 是 v2.5→v4.1 一长串增量 ALTER / 全表重建 | `core/database.py:48-404` |
| **测试只调 `create_all`**，各自 tmp_path 引擎，从不碰 `_ensure_sqlite_schema`、不碰生产库 | `tests/conftest.py:32-63` 等 |
| 模型 = create_all 的 schema 真相（测试全绿即证明模型已含全部列） | `models/*.py` |
| alembic 1.18.4 已装，但不在 `requirements.txt` | `pip show` |

## 2. 核心岔路：create_all 与 Alembic 的权威关系

`create_all(checkfirst=True)` 每次启动都按**当前模型**建表。若某张表既在模型里（create_all 会建）、又在某条 migration 里 `op.create_table`，则 create_all 抢先建好 → 该 migration 在全新库上 `CREATE TABLE` 必冲突。这是 create_all + Alembic 共存的根本矛盾，必须二选一：

### 方案 A（教科书终态）：Alembic 独占生产 schema 权威
- 生产启动只跑 `alembic upgrade head`，从生产路径移除 create_all / `_ensure_sqlite_schema`。
- baseline migration 必须**精确复刻当前全 schema**（含 `bid_alignment_items` 的部分唯一索引 + CHECK 约束 + 重建后结构）。
- 测试仍用 create_all（独立路径）。
- **风险**：baseline 必须 100% 等于 create_all+`_ensure` 的产物；autogenerate 对 SQLite 部分索引 / CHECK / server_default 有盲区，需手工补齐并逐项核对。改动启动链路，回归面大。一步到位风险高。

### 方案 B（增量、低风险，推荐本步采用）
- **保留** create_all + `_ensure_sqlite_schema` 现状不动（继续负责全新库 bootstrap 与存量库迁移到 baseline）；**冻结** `_ensure_sqlite_schema`（加注释：自 baseline 起不再新增条目，新变更一律走 Alembic）。
- 启动顺序：`create_all` → `_ensure_sqlite_schema` →（新增）`stamp baseline if 未版本化` → `upgrade head`。
- baseline migration 内容为「代表当前状态」的占位（`upgrade`/`downgrade` 皆 `pass`）——因为存量库与全新库的当前 schema 已由 create_all+`_ensure` 保证，baseline 只负责**起一个版本锚点**，其正确性不依赖复刻 schema。
- P1-3 / P1-4 等新变更 = baseline 之后的新 revision；为兼容「create_all 在全新库上已按模型建好」，新 revision 写成**幂等**（先 inspect 列/表是否存在再 ALTER/CREATE）。
  - 存量已 stamp 库：列/表不存在 → migration 正常执行。
  - 全新库：create_all 已建 → migration 幂等跳过。
- **收益**：存量生产库从此「ad-hoc ALTER」转为「有序、可 review、可回滚的 versioned migration」；不重写已验证的 bootstrap；不需完美 baseline；回归面最小。
- **代价**：migration 需写幂等守卫（轻微非惯用）；全新库上 create_all 仍是实际建表者，Alembic 在全新库上主要起版本记账。这是通向方案 A 的安全过渡态，后续可在独立批次切到 A。

> 推荐：**本步采用方案 B**，符合「速战速决 / 先稳后净」。方案 A 作为后续可选收尾，留待 baseline 复刻经充分核对后再切。

## 3. 落地清单（方案 B）

### 3.1 基础设施（不碰生产数据，可直接做）
1. `requirements.txt` 加 `alembic`（钉版本 `==1.18.4`）。
2. `apps/api/migrations/`：`alembic init` 产物（`env.py` + `script.py.mako` + `versions/`）。
3. `alembic.ini`（仓库根或 `apps/api/`）：`script_location` 指向 `apps/api/migrations`；`sqlalchemy.url` 由 `env.py` 从 `core.database.DATABASE_URL` 注入（**不写死路径**，遵守 database-safety）。
4. `env.py`：`target_metadata = Base.metadata`，并 import `apps.api.models`（触发全模型注册，供 autogenerate）；`render_as_batch=True`（SQLite ALTER 需 batch 模式）。
5. baseline revision（占位，`upgrade`/`downgrade` = `pass`，docstring 说明「代表 create_all+_ensure 现状的版本锚点」）。
6. `core/database.py`：
   - `_ensure_sqlite_schema` 顶部加冻结注释。
   - 新增 `_run_alembic_upgrade()`：若无 `alembic_version` 表 → `command.stamp(cfg, "base→baseline")`；再 `command.upgrade(cfg, "head")`。用 programmatic API，避免 shell 依赖。
   - `init_db()` 末尾调用 `_run_alembic_upgrade()`。
7. 测试不变（仍 create_all）；新增一条单测：临时库跑 `upgrade head` 不报错、`alembic_version` 落到 head。

### 3.2 生产库过渡（碰 `data/mempas.db`，按 database-safety 走）
- 备份 → 在备份副本上 dry-run `stamp baseline` + `upgrade head` → 核对表结构与行数守恒 → 用户确认 → 对生产库执行。
- 本步 baseline 是 no-op，stamp 仅写 `alembic_version`，零数据风险；但仍按流程留痕。
- **此子步在 3.1 完成并自测通过后单独执行，需显式确认。**

### 3.3 后续（本设计解锁，不在本步实现）
- P1-3：`ADD COLUMN bid_quote_lines.updated_at`（幂等 revision）。
- P1-4：`CREATE TABLE domain_audit_events`（幂等 revision）。
- 方案 A 切换（移除生产 create_all、baseline 复刻全 schema）——独立批次。

## 4. 验收
- `python -m pytest apps/api/tests -q` 与基线一致（不回归）。
- 新增迁移单测通过。
- 临时副本库 `upgrade head` 后 schema 与 create_all+_ensure 产物逐表一致（`PRAGMA table_info` 对比）。
- 生产库过渡：备份存在、`alembic_version=baseline`、业务读写正常、行数守恒。

## 5. 风险与回退
- 方案 B baseline 为 no-op → stamp/upgrade 对存量库零结构改动，回退 = 删 `alembic_version` 表恢复旧行为。
- 启动新增 `_run_alembic_upgrade` 若抛错需 fail-fast（不可静默吞，否则 schema 漂移）。
- 幂等守卫必须基于 `inspect`，禁止用 try/except 吞 DDL 错误掩盖真实失败。
