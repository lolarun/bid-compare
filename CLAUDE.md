# CLAUDE.md — MEMPAS 项目约定

## 服务启停规范

- 后端端口：**8002**，前端端口：**3000**（固定，不要改）
- 禁用 `--reload`（生产行为一致）
- 端口冲突先 kill 再重启：`netstat -ano | findstr :8002` → `taskkill /PID <pid> /F`

本地启动：

```bash
# 后端（在 repo 根目录）
python -m uvicorn apps.api.main:app --port 8002

# 前端
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

- 提交前 pre-commit 钩子会跑 `vue-tsc -b`，首次克隆需：`git config core.hooksPath .githooks`
- 不要 `--no-verify` 绕过，有 TS 报错就先修

## 设计原则

- 速战速决，不过度设计，砍掉 ROI 低的功能
- 非琐碎功能：设计 → 讨论 → 落文档 → 实现 → 测试 → 确认，不直接动手
- PDF OCR 结果不得无条件替代 Excel；品牌只作安全信号（冲突降 pending，匹配加 evidence）
