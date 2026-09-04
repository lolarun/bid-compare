#!/usr/bin/env bash
# 一次性完成：push main（触发 GitHub Actions 部署）→ 覆盖线上数据库与上传文件。
#
# 2026-09-04 建立。用户明确要求"数据库也覆盖过去"，前提是**线上尚未上线**，
# 因此覆盖属于环境初始化而不是销毁生产数据。即便如此，第 2 步仍然先备份——
# 备份成本几乎为零，而没有备份的覆盖不可回滚（.claude/rules/database-safety.md
# 「不删除、不重建用户数据，除非用户明确授权且已验证备份」）。
#
# 用法：
#   bash scripts/deploy_with_db.sh            # 全部做
#   bash scripts/deploy_with_db.sh --push-only    # 只推代码，不动数据库
#   bash scripts/deploy_with_db.sh --db-only      # 只覆盖数据库，不推代码
#
set -euo pipefail

ECS_HOST="${ECS_HOST:-root@106.14.113.209}"
REMOTE_DIR="/opt/mempas"
LOCAL_DB="data/mempas.db"
LOCAL_UPLOADS="data/uploads"

DO_PUSH=1
DO_DB=1
case "${1:-}" in
  --push-only) DO_DB=0 ;;
  --db-only)   DO_PUSH=0 ;;
  "")          ;;
  *) echo "未知参数：$1"; exit 2 ;;
esac

# ── 前置检查 ────────────────────────────────────────────────────────────────
[ -f "$LOCAL_DB" ] || { echo "找不到 $LOCAL_DB，请在仓库根目录运行"; exit 1; }

echo "=== 将要执行 ==="
[ "$DO_PUSH" = 1 ] && echo "  1) git push origin main —— 会触发 GitHub Actions 构建并部署到 $ECS_HOST"
if [ "$DO_DB" = 1 ]; then
  echo "  2) 备份线上 $REMOTE_DIR/data/mempas.db"
  echo "  3) 停 backend → 覆盖数据库($(du -h "$LOCAL_DB" | cut -f1)) + 上传文件($(du -sh "$LOCAL_UPLOADS" 2>/dev/null | cut -f1)) → 起 backend"
  echo
  echo "  ⚠ 覆盖的是本地开发库当前状态，其中包含本次会话的测试报价数据。"
fi
echo
read -r -p "确认继续？(输入 yes 继续) " ok
[ "$ok" = "yes" ] || { echo "已取消"; exit 0; }

# ── 1. 推代码（触发部署）──────────────────────────────────────────────────
if [ "$DO_PUSH" = 1 ]; then
  echo
  echo "=== git push origin main ==="
  git push origin main
  echo "已推送。部署进度：https://github.com/lolarun/bid-compare/actions"
  echo "等待镜像构建完成后再覆盖数据库，避免 backend 起来时把旧库迁移了又被覆盖。"
  read -r -p "GitHub Actions 跑完了吗？(yes 继续覆盖数据库 / 其它跳过) " done_ci
  [ "$done_ci" = "yes" ] || { echo "跳过数据库覆盖。稍后可跑：bash scripts/deploy_with_db.sh --db-only"; exit 0; }
fi

[ "$DO_DB" = 1 ] || exit 0

# ── 2. 备份线上库 ───────────────────────────────────────────────────────────
echo
echo "=== 备份线上数据库 ==="
ssh "$ECS_HOST" "cd $REMOTE_DIR/data 2>/dev/null && \
  if [ -f mempas.db ]; then \
    cp mempas.db mempas-\$(date +%F-%H%M%S).db.bak && \
    echo '已备份：' && ls -lh mempas-*.db.bak | tail -1; \
  else echo '线上暂无 mempas.db，跳过备份'; fi"

# ── 3. 停 backend → 覆盖 → 起 backend ──────────────────────────────────────
echo
echo "=== 停 backend（避免覆盖时有写入）==="
ssh "$ECS_HOST" "cd $REMOTE_DIR && docker compose -f docker-compose.prod.yml stop backend"

echo
echo "=== 覆盖数据库 ==="
scp "$LOCAL_DB" "$ECS_HOST:$REMOTE_DIR/data/mempas.db"

echo
echo "=== 同步上传文件（库里 extraction_jobs 引用着这些 PDF，不同步会 404）==="
# rsync 而不是 scp：159M 且可断点续传；--delete 不加，避免误删线上已有文件
rsync -avz --progress "$LOCAL_UPLOADS/" "$ECS_HOST:$REMOTE_DIR/data/uploads/"

echo
echo "=== 起 backend（启动时 init_db 自动跑 alembic 到 0014）==="
ssh "$ECS_HOST" "cd $REMOTE_DIR && docker compose -f docker-compose.prod.yml up -d backend"

echo
echo "=== 健康检查 ==="
sleep 6
ssh "$ECS_HOST" "curl -sf http://172.18.0.1:8100/api/health && echo '  ✓ API 正常' || \
  echo '  ⚠ 健康检查未通过，看：docker compose -f $REMOTE_DIR/docker-compose.prod.yml logs backend'"

echo
echo "=== 核对迁移与数据 ==="
ssh "$ECS_HOST" "cd $REMOTE_DIR && docker compose -f docker-compose.prod.yml exec -T backend \
  python -c \"
import sqlite3
c = sqlite3.connect('/app/data/mempas.db')
print('alembic 版本:', c.execute('select version_num from alembic_version').fetchone())
print('项目数:', c.execute('select count(*) from projects').fetchone()[0])
print('用户数:', c.execute('select count(*) from users').fetchone()[0])
print('submission_basis 表:', c.execute(\\\"select count(*) from sqlite_master where type='table' and name='submission_basis'\\\").fetchone()[0])
\" " || echo "  ⚠ 核对步骤失败，手动进容器看一眼"

echo
echo "完成。回滚数据库：ssh $ECS_HOST 'cd $REMOTE_DIR/data && cp mempas-<时间戳>.db.bak mempas.db' 后重启 backend。"
