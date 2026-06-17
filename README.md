# MEMPAS — 机电材料查询比价分析系统

## 本地开发

### 1. 激活 Git 钩子（首次克隆后执行一次）

```bash
git config core.hooksPath .githooks
```

钩子内容：提交前若有前端文件变更，自动运行 `vue-tsc -b` 严格类型检查，与 Docker 生产构建保持一致。

### 2. 后端

```bash
cd apps/api
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env   # 填写 DASHSCOPE_API_KEY 等
uvicorn apps.api.main:app --port 8000
```

### 3. 前端

```bash
cd apps/www
npm install
npm run dev   # http://localhost:3000
```

---

## 生产部署（阿里云 ECS）

```bash
ssh root@<server-ip>
cd /opt/mempas
git pull
docker compose up -d --build
```

---

## 常用命令

| 命令 | 说明 |
|------|------|
| `npm run type-check` | 仅做 TS 类型检查（不构建） |
| `npm run build` | 生产构建（vue-tsc + vite） |
| `docker compose logs -f backend` | 查看后端日志 |
| `cp data/mempas.db data/mempas-$(date +%F).bak` | 备份数据库 |
