#!/usr/bin/env bash
# MEMPAS 部署脚本 —— 与 pixel-lora 共宿主 ECS（106.14.113.209）专用。
# 见 docs/DEPLOY.md §四/§五。
#
# 调用方：
#   - GitHub Actions deploy job（push main 后自动触发，见 .github/workflows/deploy.yml）
#   - 手动救场：bash /opt/mempas/scripts/deploy.sh
#
# 前置条件（首次部署需做，见 docs/DEPLOY.md §四）：
#   1. /opt/mempas/.env 已配（ACR_REGISTRY / TAG，参考 docker-compose.prod.yml 顶部注释）
#   2. /opt/mempas/apps/api/.env 已配（DASHSCOPE_API_KEY 等，拷贝自 apps/api/.env.example）
#   3. docker login 到 ACR 已做过（credential helper 持久化，与 pixel-lora 共用同一实例）
#   4. pixel-lora 仓库那边的一次性 nginx.conf 改动已经上线（docs/DEPLOY.md §3.4）
#
# 跟 pixel-lora 的 scripts/deploy.sh 的一个关键差异：这里**不**跑独立的
# `alembic upgrade head` 步骤——MEMPAS 的迁移是 apps/api/core/database.py
# ::init_db() 在 FastAPI 启动时自动跑的（程序化配置 Alembic，不依赖
# alembic.ini），`docker compose up -d` 重启 backend 时就会顺带跑完，
# 单独再跑一次纯属多余，而且镜像里根本没拷 alembic.ini（CLI 会直接报错
# FileNotFoundError）——这是 MEMPAS 自己的既有设计，不是 pixel-lora 那种
# "显式一次性迁移" 模式，照抄会跑不通，特此记录。
set -euo pipefail

cd /opt/mempas
set -a; source .env; set +a   # ACR_REGISTRY / TAG

echo "=== pull backend image ==="
docker compose -f docker-compose.prod.yml pull backend

echo "=== restart backend（含自动迁移，见上方注释）==="
docker compose -f docker-compose.prod.yml up -d --remove-orphans

echo "=== wait api healthy ==="
sleep 5
curl -sf http://172.18.0.1:8100/api/health || echo "⚠️  健康检查未通过，看 docker compose logs backend"

echo "=== pull + extract frontend dist ==="
docker pull "${ACR_REGISTRY}/bidcom/mempas-www:${TAG:-latest}"
mkdir -p /opt/pixora/infra/nginx/html/mempas
docker run --rm -v /opt/pixora/infra/nginx/html/mempas:/out \
  "${ACR_REGISTRY}/bidcom/mempas-www:${TAG:-latest}"

echo "=== reload shared nginx（pixel-lora 的 infra-nginx-1 容器）==="
docker exec infra-nginx-1 nginx -s reload

echo "=== prune dangling images ==="
docker image prune -f

echo ""
echo "=== ✅ mempas deploy done ==="
echo "  http://bid.hotcrp.cn"
