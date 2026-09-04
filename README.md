# MEMPAS bid-compare

MEMPAS 是机电材料招标比价系统。当前产品和架构事实分别以
[`docs/spec/FUNCTIONAL.md`](docs/spec/FUNCTIONAL.md) 和
[`docs/spec/TECHNICAL.md`](docs/spec/TECHNICAL.md) 为准；历史设计、测量和撤回记录位于
[`archive/design/`](archive/design/)。开发约束和完整仓库地图见 [`CLAUDE.md`](CLAUDE.md)。
工作区治理计划见 [`PLANNING.md`](PLANNING.md)；根目录 [`TODO.md`](TODO.md)
是明确标记为冻结的历史清单，不是当前状态来源。

## Repository layout

- `apps/api/`：FastAPI 后端及唯一的 pytest 测试代码根目录。
- `apps/www/`：Vue 3 前端。
- `tests/fixtures/`：受控、带清单的 E2E 语料，不是测试代码根目录。
- `docs/spec/`：当前产品和技术规范。
- `docs/DEPLOY.md`：当前部署说明。
- `archive/`：不再维护的设计、产品资料和脚本。
- `docs/data/`：现有历史价格数据资产；迁移前仍按当前治理规则维护。
- `data/`：本地数据库、上传和诊断产物，默认不纳入版本控制。

未经清单审查的客户原件不得直接加入 Git。测试语料必须登记在
[`tests/fixtures/documents/MANIFEST.md`](tests/fixtures/documents/MANIFEST.md)，并说明来源、角色和标准答案。

## Development

```powershell
# Backend
uv sync --extra dev
uv run uvicorn apps.api.main:app --port 8020

# Frontend
npm --prefix apps/www ci
npm --prefix apps/www run dev -- --port 5120
```

不要使用 `--reload`，也不要改用临时端口。前端代理固定指向后端 `8020`。

## Verification

```powershell
uv run pytest apps/api/tests -q
npm --prefix apps/www run type-check
npm --prefix apps/www run test:unit
```

真实模型 E2E 是显式选择项；单元测试、snapshot replay 和 fresh E2E 的证据不可互相替代。

## Deployment

推送到 `main` 会触发测试、镜像构建和部署工作流。环境、密钥和首次部署步骤见
[`docs/DEPLOY.md`](docs/DEPLOY.md)。
