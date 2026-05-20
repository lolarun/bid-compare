# MEMPAS 部署指南（阿里云 ECS · 单机双容器）

> 目标：把 MEMPAS（机电材料比价分析系统）以最小成本部署到阿里云 ECS，
> 满足上线初期 ~50 在线用户 / 每分钟 5–10 次 OCR 任务的吞吐。

## 一、ECS 选型（预算优化）

| 用户数 | ECS 规格 | 月成本（华东1，按量/包月） | 备注 |
|---|---|---|---|
| ≤ 20 在线 / 1-3 OCR·min⁻¹ | `ecs.g7.xlarge` 4 vCPU / 8 GB | ~¥230/月 包年 | **推荐起步** |
| 30-50 在线 / 5-10 OCR·min⁻¹ | `ecs.g7.2xlarge` 8 vCPU / 16 GB | ~¥460/月 包年 | 同时多家上传 OCR 时升级 |
| 100+ 在线 | 见「升级路径」章节 | | 需要拆分 ECS / 上 arq |

**磁盘**：50 GB ESSD 通用型，¥18/月。SQLite + 历史上传 OCR 文件足够用 6-12 个月。

**带宽**：5 Mbps 按量计费，~¥30/月。OCR 上传单文件 PDF 通常 < 2 MB，5 Mbps 够。

**总计起步成本**：~¥280/月。

## 二、首次部署步骤

```bash
# 1. SSH 登录 ECS
ssh root@<your-ecs-public-ip>

# 2. 安装 Docker + Compose（CentOS / Anolis 8）
yum install -y docker-ce docker-compose-plugin
systemctl enable --now docker

# 3. 防火墙：仅开放 80/443/22
firewall-cmd --add-port=80/tcp --permanent
firewall-cmd --add-port=443/tcp --permanent  # 上线 TLS 后
firewall-cmd --reload

# 4. 拉代码
git clone <your-repo-url> /opt/mempas
cd /opt/mempas

# 5. 配置环境变量（不要 commit！）
cp apps/api/.env.example apps/api/.env
vim apps/api/.env
# 填入 DASHSCOPE_API_KEY=sk-xxxx 等

# 6. 启动
docker compose up -d --build

# 7. 验证
curl http://127.0.0.1/api/health
# {"status":"ok","service":"mempas","llm_provider":"qwen_vl"}

curl http://127.0.0.1/api/health/queue
# {"active_threads":0,"queue_depth":0,"max_workers":8}

# 8. 浏览器访问
# http://<your-ecs-public-ip>/
```

## 三、目录结构（运行时）

```
/opt/mempas/
├── apps/api/.env           ← 密钥（gitignored，必须手动 chmod 600）
├── data/                   ← 持久卷（绑定到 backend 容器 /app/data）
│   ├── mempas.db           ← SQLite 主库
│   ├── mempas.db-wal       ← SQLite WAL
│   └── uploads/2026XXXX/   ← OCR 上传文件（按日期分目录）
└── docker-compose.yml
```

**关键**：`data/` 永远不要 commit，永远要备份。

## 四、日常运维

### 备份

```bash
# 简单方案：每日 cron
crontab -e
# 添加：
0 2 * * * cd /opt/mempas && cp data/mempas.db data/mempas-$(date +\%F).db.bak && find data/mempas-*.db.bak -mtime +30 -delete
```

### 监控指标（重要！决定何时升级）

| 端点 | 含义 | 升级阈值 |
|---|---|---|
| `GET /api/health` | LLM provider 是否可用 | `llm_provider != "qwen_vl"` → 检查 API key |
| `GET /api/health/queue` | OCR 任务池状态 | `queue_depth > max_workers` 持续 5 分钟 → 升级方案 |

建议接 Aliyun CMS 或 Prometheus，把 `queue_depth > 0` 的持续时间监控起来。

### 日志

```bash
# 实时日志
docker compose logs -f backend
docker compose logs -f frontend

# 最近 1 小时错误
docker compose logs --since 1h backend | grep -iE "error|exception"
```

### 更新代码

```bash
cd /opt/mempas
git pull
docker compose up -d --build  # 仅重建发生变化的镜像
```

### 容器资源限制

`docker-compose.yml` 已设置：
- backend: 最多 3 CPU / 5 GB RAM
- frontend: 最多 0.5 CPU / 256 MB RAM

如果 ECS 升级到 8 vCPU 16 GB，把 backend 改成 `6.0 CPU / 12 GB`。

## 五、安全清单

- [x] `apps/api/.env` 权限 `chmod 600`
- [x] backend 容器不暴露 8000 端口到 host（compose 用 `expose`，不是 `ports`）
- [x] nginx `client_max_body_size 10M` 防大文件 DoS
- [x] backend 容器以非 root 用户 `mempas:1000` 运行
- [ ] **TLS**：上线后从阿里云申请免费 SSL 证书，挂在 SLB / CDN 或者改 `nginx.conf` 加 443
- [ ] **fail2ban**：考虑给 nginx 加 IP 限速 / 失败次数限制
- [ ] **定期更新基础镜像**：`docker compose pull && docker compose up -d`

## 六、容量与升级路径

### 当前架构（成本最低）
```
ECS (4 vCPU / 8 GB)
├── nginx       (1 容器, 0.5 CPU)
└── uvicorn × 4 workers (1 容器, 3 CPU)
    └── ThreadPoolExecutor × 8 threads (per worker)
    = 最多 32 个并发 OCR 任务（IO-bound，CPU 闲）
```

**支撑能力**：~50 在线用户，5-10 次 OCR/分钟，平均响应 < 2 秒。

### 升级触发点

监控 `GET /api/health/queue`：

| 现象 | 处理 |
|---|---|
| `queue_depth = 0` 长期 | 一切正常 |
| `queue_depth = 1-3` 偶发 | 正常波动 |
| `queue_depth > max_workers` 持续 5+ 分钟 | **升级到方案 β** |
| backend 容器 CPU > 70% 持续 | **升级 ECS 规格** |
| OCR 平均耗时翻倍 | 检查 Qwen-VL 限流，考虑批量降级模型 |

### 升级方案 β：上 arq + 自部署 Redis（同 ECS）

成本：¥0（Redis 跑在同 ECS）
工程量：半天

1. 在 docker-compose.yml 加 `redis:` 服务（image `redis:7-alpine`，资源 0.5 CPU / 512 MB）
2. 安装 `arq` 依赖
3. 把 `submit_extraction` 换成 `redis.enqueue('run_job', job_id)`
4. 新增 `worker` 容器跑 `arq apps.api.workers.WorkerSettings`
5. backend 和 worker 共享代码镜像，只是 ENTRYPOINT 不同

### 升级方案 γ：拆分 ECS

- ECS A：nginx + backend (HTTP 路径，纯 API 服务)
- ECS B：worker × N（OCR 任务专用，可弹性伸缩）
- 阿里云 Redis 企业版：~¥150/月起，比自部署可靠

什么时候需要：100+ 并发用户 / 每分钟 30+ OCR。当前业务规模不需要。

## 七、回滚

```bash
cd /opt/mempas
git log --oneline -10            # 找上一个稳定 commit
git checkout <stable-sha>
docker compose up -d --build
```

数据库回滚（如果有破坏性 schema 变更）：

```bash
docker compose down
cp data/mempas-2026-05-19.db.bak data/mempas.db
docker compose up -d
```

## 八、常见问题

**Q: `llm_provider` 显示 `mock`？**
A: API key 未加载。检查 `apps/api/.env` 是否存在且包含 `DASHSCOPE_API_KEY=sk-xxx`。

**Q: 上传 OCR 后 30 秒还是 pending？**
A: `docker compose logs backend` 看是否有 Qwen-VL API 错误。检查 ECS 出网。
   检查 `/api/health/queue` 看是不是 queue_depth 在堆积。

**Q: SQLite 报 `database is locked`？**
A: WAL 已开（`PRAGMA journal_mode=WAL`），同进程内 SQLAlchemy 池化连接。
   如果还出现，多半是有外部进程在写 DB（备份脚本？）。或者真到 SQLite
   的写并发瓶颈了 → 升级方案 γ（独立 PostgreSQL）。

**Q: 怎么禁用某用户 / 删除测试数据？**
A: 进入 backend 容器：`docker compose exec backend python` → 用 SQLAlchemy 操作。
   或者：`docker compose exec backend sqlite3 /app/data/mempas.db`。
