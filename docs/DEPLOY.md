# MEMPAS 部署指南（阿里云 ECS · 与 pixel-lora 共宿主 · GitHub Actions）

> **状态 — 2026-08-18 更新，PLAN（待实现，未执行）。**
> 旧版单机方案（独立 ECS `101.37.166.68`、手动 `git pull && docker compose up -d --build`）
> 已确认废弃——那台机器现在连不上，不再维护。新方案：**bid-compare 迁移到
> pixel-lora 项目正在用的那台 ECS（`106.14.113.209`）上共宿主**，构建方式也从
> "ECS 上本地 build" 换成"GitHub Actions 构建镜像 → 推阿里云 ACR → SSH 触发
> ECS 拉镜像重启"，跟 pixel-lora 现有的 `.github/workflows/deploy.yml` 同一套
> 模式（该文件路径：`C:\Users\Justin\codes\repos\pixel-lora\.github\workflows\deploy.yml`）。
>
> 本文档是**规划稿**：架构、workflow、compose 文件都是设计出来待评审的，
> 还没有落地执行——没有跑过 `docker build`、没有改过 pixel-lora 仓库、没有
> SSH 上过 106.14.113.209。旧版一~八节的内容（ECS 选型/首次部署/运维手册等）
> 保留在下方标注为"已废弃"的区块，供历史对照与回滚参考，不再是当前指引。
>
> **域名已定**（2026-08-18）：`bid.hotcrp.cn`，暂时 HTTP-only（无 SSL 证书，
> 走 80 端口，443/证书留到后续再补，§3.4 已更新）。

## 目录

- [一、迁移背景与决策](#一迁移背景与决策)
- [二、目标架构](#二目标架构)
- [三、需要新建/修改的文件](#三需要新建修改的文件)
- [四、迁移步骤（首次切换）](#四迁移步骤首次切换)
- [五、日常更新（迁移后）](#五日常更新迁移后)
- [六、运维（备份/监控/回滚）](#六运维备份监控回滚)
- [附录：旧版单机方案（已废弃，仅供历史对照）](#附录旧版单机方案已废弃仅供历史对照)

---

## 一、迁移背景与决策

| 项 | 旧方案 | 新方案 |
|---|---|---|
| ECS | 独立一台，`101.37.166.68`（**已废弃，连不上，不再维护**） | 复用 pixel-lora 的 `106.14.113.209` |
| 部署路径 | ECS 上 `/opt/mempas`，`git pull` 后本地 `docker compose up -d --build` | ECS 上 `/opt/mempas`（目录名不变），只 `docker compose pull` + `up -d`，不在 ECS 上跑 build |
| 镜像仓库 | 无（本地 build，不走镜像仓库） | 复用 pixel-lora 现有的阿里云 ACR 实例，新建命名空间 `bidcom`（不是 pixel-lora 用的 `pixora`，镜像互不混淆） |
| CI/CD | 无（纯手动 SSH） | GitHub Actions：push main → build → push ACR → SSH 触发 ECS 部署，结构照抄 pixel-lora 的 `deploy.yml` |
| 反向代理 | bid-compare 自己的 nginx 容器独占 80/443 | **接入 pixel-lora 现有的共享 nginx 容器**，新增一个 server block（bid-compare 不再自带对外监听 80/443 的 nginx 容器，见 §2.2）——这是**唯一**要碰 pixel-lora 仓库的地方，且只做一次，之后的每次发布都不再涉及 |
| 数据库 | SQLite，卷挂载在 `/opt/mempas/data/` | 不变——两个项目都不用 MySQL/RDS，SQLite 数据完全独立于 pixel-lora，物理上same host 但不共享任何数据 |

**为什么复用 ACR 实例而不是新开一个**：pixel-lora 已经在付费维护一个 ACR 实例，多开一个命名空间几乎零边际成本；新开实例则是重复的固定月费。风险在于两个项目的镜像清理策略/配额要共享，§3.3 有对应处理。

**为什么复用同一台 ECS 而不是新开**：bid-compare 是内部工具（预估 ≤50 在线用户、5-10 OCR/分钟），资源需求远小于当初为它单独开的 4C8G。pixel-lora 的 ECS 是 8C16G，且推理/训练都在 PAI-EAS/PAI-DLC 上跑，ECS 本身只扛 nginx+API+两个轻量 worker，理论上有富余——**但这是理论评估，迁移前必须实机核实**（见 §4 步骤 1），不能假设。

**两个项目独立到什么程度**（2026-08-18 确认）：镜像仓库（ACR 实例）和物理宿主机（ECS）共用，其余全部独立——各自的 GitHub 仓库、各自的 Actions workflow、各自的 `docker compose` 项目（`/opt/pixora` vs `/opt/mempas`）、各自的数据库，互不感知对方的部署节奏。**唯一的例外、也是唯一需要碰 pixel-lora 仓库的地方**：因为宿主机 80/443 端口只能被一个进程监听，现在是 pixel-lora 的 nginx 容器占着，`bid.hotcrp.cn` 要走标准 80 端口就必须在这个共享 nginx 里加一条路由规则。这是**一次性的最小改动**（§3.4 给的那个 server block，加一次就够）——加完之后，bid-compare 之后每次发布（改代码、加功能）都只触发自己仓库的 GitHub Actions，不会再产生任何新的 pixel-lora 仓库改动。

## 二、目标架构

### 2.1 整体拓扑

```
                     ECS 106.14.113.209（8C16G，与 pixel-lora 共宿主）
                     ┌──────────────────────────────────────────────┐
公网 ── 80/443 ──►   │  共享 nginx（pixel-lora 现有容器，本次只加配置）│
                     │    ├─ server: aiguozhanbijin.com.cn      → pixora-www dist
                     │    ├─ server: mng.aiguozhanbijin.com.cn  → pixora-mng dist
                     │    ├─ server: api.aiguozhanbijin.com.cn  → pixora-api:8000
                     │    ├─ server: m.aiguozhanbijin.com.cn    → pixora-h5 dist
                     │    └─ server: bid.hotcrp.cn (HTTP-only)   → mempas-www dist
                     │                                    │
                     │                          proxy /api/ ▼
                     │                          172.18.0.1:8100 (bridge-gateway-only)
                     │                                    │
                     │              /opt/mempas/docker-compose.prod.yml
                     │              ┌──────────────────────────────┐
                     │              │  backend  (mempas-api)       │
                     │              │  发布端口 172.18.0.1:8100→8000│
                     │              │  卷: /opt/mempas/data         │
                     │              └──────────────────────────────┘
                     └──────────────────────────────────────────────┘
```

**关键设计决策（2026-08-18 已用真实 ECS 数据核实，不再是推测）**：实测 pixel-lora 的共享 nginx 容器（真实名 `infra-nginx-1`，不是之前猜的 `pixora-nginx-1`——compose 项目名取自目录名 `infra/`）跑在标准 bridge 网络 `infra_default`，网关 `172.18.0.1`（`docker network inspect infra_default` 实测确认，非固定值，见下方"已知脆弱点"）。容器内的 `127.0.0.1` 指向容器自己，不是宿主机——一开始设想的"绑 127.0.0.1、靠回环互通"这条路**走不通**，已实测排除。

改为：bid-compare 的 backend 容器**绑定 `172.18.0.1:8100`**（docker bridge 网关地址，不是 `0.0.0.0`）。选它而不是 `0.0.0.0:8100` 的原因：ECS 上的 ufw 只放行了 22/80/443/2222（`ufw status` 实测确认），但 Docker 自己的转发规则（`DOCKER-FORWARD`/`DOCKER-USER` 链）默认不受 ufw INPUT 链约束，这是一个众所周知的 docker+ufw 坑——`0.0.0.0:8100` 有没有被真正挡在公网外**不能光看 ufw 的输出就下结论**。绑定到网关 IP 而不是 `0.0.0.0`，直接从监听地址层面排除了公网可达的可能，不依赖 ufw/iptables 规则对不对——更省心也更保险。

跟"把 mempas 容器接进 pixora 的 docker network"比：绑网关 IP 不需要 bid-compare 的 compose 文件知道 pixel-lora 网络的名字，耦合更低；**已知脆弱点**：如果 pixel-lora 哪天完整 `docker compose down`（不只是 `up -d`）重建了 `infra_default` 网络，网关 IP 理论上可能变（实践中，宿主机上只有这一个自定义网络、没有其他网络抢网段，几乎总是分到同一个网段，但不是 100% 保证）。一旦真的变了，症状是 bid.hotcrp.cn 的 `/api/` 502，改 nginx.conf 里那一行 IP 就好，影响面很小、好排查——比绑定 `0.0.0.0` 赌 ufw/iptables 配置正确、或者把两个项目的 docker network 耦合在一起，风险都更低。

### 2.2 为什么 bid-compare 不能保留自己的 nginx 容器

`docker-compose.yml`（独立单机部署方案）里 frontend 服务发布 `80:80`，这在**独占一台 ECS**时没问题；共宿主后 80/443 已经被 pixel-lora 的 nginx 占用，两个 nginx 容器不能同时监听同一宿主端口。

方案：仿照 pixel-lora 的 `www.Dockerfile`/`mng.Dockerfile` 模式——前端镜像只做"builder"，`CMD` 把 `dist/` 拷到挂载的 `/out`（宿主机目录），不再自带 nginx 运行时；静态文件由**共享 nginx** 读取。

**2026-08-20 决策**：不单独维护一个 `Dockerfile.builder` 文件——直接改 `apps/www/Dockerfile` 本身，把原来的"Stage 2：nginx 运行时"注释掉（不是删掉），默认产物就是 builder-only 镜像。哪天 MEMPAS 脱离共宿主、重新独占一台 ECS，取消注释即可恢复。**代价**：这次改动之后 `docker-compose.yml`（独立单机方案，见附录）暂时用不了——它期望这个 Dockerfile 产出常驻监听 80 的 nginx 容器，现在默认产出的是构建完就退出的一次性 builder，两者不兼容。真要用回独立单机方案时，记得先取消注释 Stage 2。

### 2.3 命名空间与镜像

| bid-compare 镜像 | ACR 路径 |
|---|---|
| 后端 | `<ACR_REGISTRY>/bidcom/mempas-api:latest` / `:<sha>` |
| 前端（builder，产出 dist） | `<ACR_REGISTRY>/bidcom/mempas-www:latest` / `:<sha>` |

`<ACR_REGISTRY>` 复用 pixel-lora 用的同一个实例地址（VPC 内网域名版本用于 ECS 拉镜像，公网版本用于 GitHub Actions 推镜像——具体两个地址值需要从 pixel-lora 的 GitHub Secrets `ACR_REGISTRY` 里取，我这边看不到实际值，只看到 workflow 里怎么引用它）。

## 三、需要新建/修改的文件

### 3.1 `.github/workflows/deploy.yml`（新建，bid-compare 仓库）

结构照抄 pixel-lora 的 `deploy.yml`，去掉它的 5 镜像 path-filter 矩阵（bid-compare 只有 2 个镜像，没必要那么复杂），简化成固定两镜像：

```yaml
name: build-and-deploy

on:
  push:
    branches: [main]
  workflow_dispatch:

concurrency:
  group: deploy-${{ github.ref }}
  cancel-in-progress: false

jobs:
  build:
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        include:
          - image: mempas-api
            dockerfile: apps/api/Dockerfile
          - image: mempas-www
            dockerfile: apps/www/Dockerfile   # Stage 2 已注释掉，默认产出 builder-only 镜像，见 §2.2
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-buildx-action@v3
      - uses: docker/login-action@v3
        with:
          registry: ${{ secrets.ACR_REGISTRY }}
          username: ${{ secrets.ACR_USERNAME }}
          password: ${{ secrets.ACR_PASSWORD }}
      - uses: docker/build-push-action@v6
        with:
          context: .
          file: ${{ matrix.dockerfile }}
          push: true
          tags: |
            ${{ secrets.ACR_REGISTRY }}/bidcom/${{ matrix.image }}:latest
            ${{ secrets.ACR_REGISTRY }}/bidcom/${{ matrix.image }}:${{ github.sha }}
          cache-from: type=gha,scope=${{ matrix.image }}
          cache-to: type=gha,scope=${{ matrix.image }},mode=max

  deploy:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - uses: appleboy/ssh-action@v1
        with:
          host: ${{ secrets.ECS_HOST }}
          username: root
          key: ${{ secrets.ECS_SSH_KEY }}
          script: |
            cd /opt/mempas
            git fetch origin main
            git reset --hard origin/main
            bash scripts/deploy.sh
```

**需要的 GitHub Secrets**（bid-compare 仓库自己的 Settings → Secrets，不是复用 pixel-lora 仓库的 secrets——两个仓库分开配，值可以相同）：

| Secret | 说明 | 是否可直接复用 pixel-lora 的值 |
|---|---|---|
| `ACR_REGISTRY` | 同一个 ACR 实例地址 | ✅ 直接抄 |
| `ACR_USERNAME` / `ACR_PASSWORD` | ACR 访问凭证 | ✅ 直接抄（凭证是实例级别，命名空间级 RBAC 如果开了要单独确认 `bidcom` 命名空间有没有推送权限） |
| `ECS_HOST` | `106.14.113.209` | ✅ |
| `ECS_SSH_KEY` | SSH 私钥 | ⚠️ 可以复用 pixel-lora 的 `infra/ssh/pixora_deploy`，也可以新开一把专属 bid-compare 的 deploy key（更小权限面，推荐但非必须） |

### 3.2 `apps/www/Dockerfile`（修改，不新建独立文件）

2026-08-20 决策：不单独维护一个 `Dockerfile.builder`——直接改现有的 `apps/www/Dockerfile`，把"Stage 2：nginx 运行时"整段注释掉（保留在文件里，不删），默认 `docker build -f apps/www/Dockerfile .` 产出的就是 builder-only 镜像（Stage 1 加了一行 `CMD`，把 `dist/` 拷到挂载的 `/out`）：

```dockerfile
FROM node:20-alpine AS build
WORKDIR /app
COPY apps/www/package.json apps/www/package-lock.json* ./
RUN npm config set registry https://registry.npmmirror.com && \
    npm ci --prefer-offline --no-audit --no-fund
COPY apps/www/ ./
RUN npm run build
CMD ["sh", "-c", "rm -rf /out/* && cp -r /app/dist/. /out/ && echo 'mempas-www dist copied to /out'"]

# ─── Stage 2: nginx runtime（已注释，独立单机部署时取消注释用）───────────
# FROM nginx:1.27-alpine AS runtime
# COPY --from=build /app/dist /usr/share/nginx/html
# COPY apps/www/nginx.conf /etc/nginx/conf.d/default.conf
# HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
#     CMD wget -q --spider http://127.0.0.1/ || exit 1
# EXPOSE 80
```

**代价**（§2.2 已提过一次）：`docker-compose.yml`（独立单机部署方案，见附录）现在**用不了**——它期望这个文件产出常驻监听 80 的 nginx 容器，现在默认产出的是构建完就退出的 builder。真要切回独立单机方案，先取消注释 Stage 2。

### 3.3 `docker-compose.prod.yml`（新建，bid-compare 仓库根目录）

```yaml
# 拉镜像版，不在 ECS 上 build——配合 GitHub Actions 产出的镜像使用。
# 跟现有 docker-compose.yml（本地 build 版）并存，互不影响；`scripts/deploy.sh`
# （§4 新建）用这个文件。
services:
  backend:
    image: ${ACR_REGISTRY}/bidcom/mempas-api:${TAG:-latest}
    container_name: mempas-api
    restart: unless-stopped
    env_file:
      - apps/api/.env
    environment:
      UPLOAD_DIR: /app/data/uploads
      EXTRACTION_MODE: thread
      EXTRACTION_THREAD_POOL_SIZE: "8"
    volumes:
      - ./data:/app/data
    ports:
      # 绑 pixel-lora 共享 nginx 所在 bridge 网络的网关地址——不是 0.0.0.0，
      # 不依赖 ufw/iptables 配置对不对，从监听地址层面就排除了公网可达
      # （实测值见 §2.1；如果 172.18.0.1 不再是 infra_default 的网关，
      # `docker network inspect infra_default` 重新核实后改这里）。
      - "172.18.0.1:8100:8000"
    deploy:
      resources:
        limits:
          cpus: "1.5"
          memory: 2.5G
```

不再需要 `frontend` 服务——静态文件由 `scripts/deploy.sh`（§4）用一次性容器提取到共享 nginx 的挂载目录，不是常驻服务。

### 3.4 访问域名 —— 已定：`bid.hotcrp.cn`，暂 HTTP-only

2026-08-18 确认：域名用 `bid.hotcrp.cn`，**暂时不配 SSL，走 80 端口 HTTP**（不是
`.com.cn`，所以不是 pixel-lora `aiguozhanbijin.com.cn` 的子域名，是完全独立的
域名——DNS 解析、ICP 备案要单独核实，不能借用 pixel-lora 那边已备案的身份）。

**遗留待办**（不阻塞当前部署，记在这里避免遗忘）：
- 确认 `bid.hotcrp.cn` 的 DNS A 记录已指向 `106.14.113.209`（迁移前必须做，
  否则域名访问不通）。
- 确认这个域名走中国大陆 ECS 公网访问是否需要单独 ICP 备案（`docs/contract/
  ICP备案材料及流程.md` 有材料清单，走的是新域名流程，不是子域名共享备案）。
  HTTP-only 不代表不需要备案——备案要求跟是否有 SSL 无关，是"域名解析到大陆
  服务器 + 公网可访问"就要备案。
- 后续补 SSL：证书申请 + nginx 加 443 server block + `location / { return 301
  https://$host$request_uri; }` 跳转，届时参照 pixel-lora `nginx.conf` 里
  桌面端 server block 的写法（`docs/DEPLOY.md` 编写时已读过该文件）。

对应的共享 nginx server block（§4 步骤 4 落地时用）：

```nginx
server {
    listen 80;
    server_name bid.hotcrp.cn;

    client_max_body_size 100M;   # OCR 上传（现有 apps/www/nginx.conf 同款限制）

    location /api/ {
        proxy_pass         http://172.18.0.1:8100/api/;
        proxy_http_version 1.1;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_buffering    off;
        proxy_read_timeout 120s;
        proxy_send_timeout 120s;
    }

    location / {
        root /usr/share/nginx/html/mempas;
        try_files $uri $uri/ /index.html;
    }
}
```

## 四、迁移步骤（首次切换）

> 步骤 1-2 已用真实 ECS 数据核实完成（结果已写进 §2.1，下面标 ✅）。

1. ✅ **ECS 资源余量**：实测 8 vCPU / 14GB 可用，现有 5 个 pixel-lora 容器合计 CPU <1%、内存约 1GB，加 bid-compare 预估的 1.5C/2.5G 后完全在余量内。磁盘 118G 用 73G，41G 可用。
2. ✅ **网络可达性**：已实测 §2.1 的结论——`infra_default` 网关 `172.18.0.1`，方案已定为绑网关 IP（不是回环地址方案，那个已被证实走不通）。
3. **DNS**：确认 `bid.hotcrp.cn` 的 A 记录指向 `106.14.113.209`（§3.4 遗留待办第一条）——**这一步需要你在域名服务商那边操作，我这边看不到 DNS 控制台**。
4. **【一次性】在 pixel-lora 仓库**新增/修改（这是唯一需要碰对方仓库的地方，见 §1"两个项目独立到什么程度"；改一次之后，bid-compare 之后所有发布都不会再触发这个仓库的任何改动）：
   - `infra/nginx/nginx.conf`：新增 §3.4 给出的 `bid.hotcrp.cn` server block（`proxy_pass http://172.18.0.1:8100/api/;`，已用实测网关 IP，不用再改）。需要在 `docker-compose.prod.yml` 的 nginx 服务 `volumes` 里加一行 `./nginx/html/mempas:/usr/share/nginx/html/mempas:ro` 挂载。
   - `scripts/deploy.sh`：加一段跟 www/mng/h5 一样的"pull mempas-www builder 镜像 → 提取 dist → nginx reload"逻辑——但这段其实应该放在 **bid-compare 自己的** `scripts/deploy.sh`（§4 步骤 6 新建）里更合理，因为 mempas 的发布节奏跟 pixora 的发布节奏是独立的，不应该耦合进 pixora 的部署脚本触发。**建议**：bid-compare 自己的 deploy.sh 提取 dist 到 `/opt/pixora/infra/nginx/html/mempas`（写到 pixora 的目录里）+ `docker exec` pixora 的 nginx 容器 reload——这样两边部署互相独立触发，只共享"nginx 读哪个目录"这一份配置。
5. 在 GitHub 仓库 Settings 配置 §3.1 表格里的 4 个 Secrets。
6. **新建 `scripts/deploy.sh`**（bid-compare 仓库）：

   ```bash
   #!/usr/bin/env bash
   set -euo pipefail
   cd /opt/mempas
   set -a; source .env; set +a   # ACR_REGISTRY / ACR_NAMESPACE=bidcom / TAG

   echo "=== pull backend image ==="
   docker compose -f docker-compose.prod.yml pull backend

   echo "=== run alembic migrations（one-shot，幂等）==="
   docker compose -f docker-compose.prod.yml run --rm --no-deps backend \
     alembic upgrade head

   echo "=== restart backend ==="
   docker compose -f docker-compose.prod.yml up -d --remove-orphans

   echo "=== pull + extract frontend dist ==="
   docker pull "${ACR_REGISTRY}/bidcom/mempas-www:${TAG:-latest}"
   docker run --rm -v /opt/pixora/infra/nginx/html/mempas:/out \
     "${ACR_REGISTRY}/bidcom/mempas-www:${TAG:-latest}"

   echo "=== reload shared nginx (pixora 容器) ==="
   docker exec infra-nginx-1 nginx -s reload   # 实测确认的真实容器名（compose 项目名取自目录 infra/，不是仓库名 pixora）

   echo "=== ✅ mempas deploy done ==="
   ```

   最后一行的容器名是猜的（`docker compose` 默认命名规则 `<项目名>-<服务名>-<序号>`），**首次执行前需要在 ECS 上 `docker ps` 核实真实容器名**再改这个脚本。
7. **首次手动跑一遍全流程**（不经 GitHub Actions，本地/ECS 上手动执行每一步），确认走通，再让 GitHub Actions 接管。
8. 全部验证通过后，**退役** `101.37.166.68`（释放实例，若还在计费）。

## 五、日常更新（迁移后）

跟 pixel-lora 一样：push 到 `main` 分支即自动构建+部署，不再需要手动 SSH：

```bash
git push origin main
# GitHub Actions 自动：build → 推 ACR → SSH 触发 ECS 跑 scripts/deploy.sh
```

手动强制重部署（比如只改了 `.env`、代码没变）：GitHub 仓库 Actions 页面手动触发 `workflow_dispatch`，或者直接 SSH 跑 `bash /opt/mempas/scripts/deploy.sh`。

## 六、运维（备份/监控/回滚）

以下沿用旧方案，路径不变（`/opt/mempas/data/`）：

- **备份**：cron `0 2 * * * cd /opt/mempas && cp data/mempas.db data/mempas-$(date +\%F).db.bak`，见附录原文。
- **监控**：`GET /api/health`、`GET /api/health/queue`，阈值判断不变。
- **回滚**：
  - 代码：`git checkout <sha> && bash scripts/deploy.sh`，前提是对应 sha 的镜像还在 ACR 里没被清理——ACR 保留策略需要跟 pixel-lora 那边核对，共用实例意味着清理策略也是共用的，不能自己单独设置。
  - 数据库：`cp data/mempas-<date>.db.bak data/mempas.db`，跟旧方案不变。

---

## 附录：旧版单机方案（已废弃，仅供历史对照）

> 以下是 2026-05 版本的原始内容，**101.37.166.68 已废弃不再维护**，仅保留
> 供历史对照、以及"共享 ECS 方案万一要回退到独立单机"时参考。ECS 选型的
> 成本数字、SQLite 运维经验（WAL / 备份 / 升级触发点）依然有效，只是"部署
> 到哪台机器、怎么构建镜像"这两件事已经被上面 §一~六 取代。

### 原·零、日常快速更新（已废弃）

```bash
ssh root@101.37.166.68 "cd /opt/mempas && git pull && docker compose up -d --build"
```

### 原·一、ECS 选型（预算优化，数字依然有参考价值）

| 用户数 | ECS 规格 | 月成本（华东1，按量/包月） | 备注 |
|---|---|---|---|
| ≤ 20 在线 / 1-3 OCR·min⁻¹ | `ecs.g7.xlarge` 4 vCPU / 8 GB | ~¥230/月 包年 | 独立部署时的起步规格 |
| 30-50 在线 / 5-10 OCR·min⁻¹ | `ecs.g7.2xlarge` 8 vCPU / 16 GB | ~¥460/月 包年 | 同时多家上传 OCR 时升级 |

磁盘：50 GB ESSD，¥18/月。带宽：5 Mbps 按量，~¥30/月。

### 原·三、目录结构（运行时，共宿主后依然适用）

```
/opt/mempas/
├── apps/api/.env           ← 密钥（gitignored，必须手动 chmod 600）
├── data/                   ← 持久卷（绑定到 backend 容器 /app/data）
│   ├── mempas.db           ← SQLite 主库
│   ├── mempas.db-wal       ← SQLite WAL
│   └── uploads/2026XXXX/   ← OCR 上传文件（按日期分目录）
└── docker-compose.yml / docker-compose.prod.yml
```

**关键**：`data/` 永远不要 commit，永远要备份。

### 原·六、容量与升级路径（判断标准依然适用）

监控 `GET /api/health/queue`：

| 现象 | 处理 |
|---|---|
| `queue_depth = 0` 长期 | 一切正常 |
| `queue_depth = 1-3` 偶发 | 正常波动 |
| `queue_depth > max_workers` 持续 5+ 分钟 | 升级方案（加 arq + Redis，同 ECS） |
| backend 容器 CPU > 70% 持续 | 找 pixel-lora 商量 ECS 整体升级规格，不要单独升 bid-compare 这部分 |

### 原·八、常见问题（不变）

**Q: `llm_provider` 显示 `mock`？**
A: API key 未加载。检查 `apps/api/.env` 是否存在且包含 `DASHSCOPE_API_KEY=sk-xxx`。

**Q: SQLite 报 `database is locked`？**
A: WAL 已开（`PRAGMA journal_mode=WAL`）。如果还出现，多半是有外部进程在写 DB（备份脚本？），或者真到 SQLite 的写并发瓶颈了。
