# CLAUDE.md — MEMPAS 机电材料查询比价分析系统

## 本地开发

### 首次克隆

```bash
git config core.hooksPath .githooks   # 激活 pre-commit 钩子（vue-tsc -b）
```

### 后端

```bash
cd apps/api
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env   # 填写 DASHSCOPE_API_KEY 等
```

- 端口：**8000**（固定，不要改）
- 禁用 `--reload`（保持与生产行为一致）
- 启动：`python -m uvicorn apps.api.main:app --port 8000`（在 repo 根目录运行）
- 端口冲突先 kill 再重启：`netstat -ano | findstr :8000` → `taskkill /PID <pid> /F`

### 前端

```bash
cd apps/www && npm run dev   # → http://localhost:3000
```

## 生产部署

**服务器**：`101.37.166.68`，路径：`/opt/mempas`

```bash
# 完整流程：先 push，再 SSH 进去一步到位
ssh root@101.37.166.68 "cd /opt/mempas && git pull && docker compose up -d --build"
```

确认部署成功：

```bash
ssh root@101.37.166.68 "cd /opt/mempas && docker compose ps && curl -s http://127.0.0.1/api/health"
```

详细说明：[docs/DEPLOY.md](docs/DEPLOY.md)

## 代码提交

- 提交前 pre-commit 钩子会跑 `vue-tsc -b`，有 TS 报错就先修
- 不要 `--no-verify` 绕过

## 常用命令

| 命令 | 说明 |
|------|------|
| `npm run type-check` | 仅做 TS 类型检查（不构建） |
| `npm run build` | 生产构建（vue-tsc + vite） |
| `docker compose logs -f backend` | 查看后端日志 |
| `cp data/mempas.db data/mempas-$(date +%F).bak` | 备份数据库 |

## 设计原则

- 速战速决，不过度设计，砍掉 ROI 低的功能
- 非琐碎功能：设计 → 讨论 → 落文档 → 实现 → 测试 → 确认，不直接动手
- PDF OCR 结果不得无条件替代 Excel；品牌只作安全信号（冲突降 pending，匹配加 evidence）
