# MEMPAS frontend — production image
#
# 2026-08-20：默认产物改成 builder-only（跟 pixel-lora 共宿主的架构要求，
# 见 docs/DEPLOY.md §2.2——那台 ECS 的 80/443 已经被 pixel-lora 自己的
# nginx 占了，MEMPAS 不能再带一个监听同端口的 nginx 容器，只能把 dist/
# 产物交给共享 nginx 读）。Stage 2（完整 nginx 运行时）保留在下面、注释掉，
# 不是删掉——MEMPAS 哪天脱离共宿主、重新独占一台 ECS 时，取消注释即可
# 恢复成独立可跑的镜像，`docker-compose.yml`（独立单机部署方案）到时候
# 也要连带恢复能用（现在这个改动之后，`docker-compose.yml` 的 frontend
# 服务暂时是不能用的——它期望这个 Dockerfile 产出一个常驻监听 80 的
# nginx 容器，而不是构建完就退出的一次性 builder，见下面注释）。

# ─── Stage 1: build（当前唯一生效的 stage）──────────────────────────────
FROM node:20-alpine AS build

WORKDIR /app

# Cache dependency install separately from source changes
COPY apps/www/package.json apps/www/package-lock.json* ./
RUN npm config set registry https://registry.npmmirror.com && \
    npm ci --prefer-offline --no-audit --no-fund

COPY apps/www/ ./
RUN npm run build

# builder-only 用法：docker run --rm -v <host路径>:/out <image>
# 把 dist/ 拷到挂载的宿主机目录，交给共享 nginx 读，容器本身不常驻。
CMD ["sh", "-c", "rm -rf /out/* && cp -r /app/dist/. /out/ && echo 'mempas-www dist copied to /out'"]


# ─── Stage 2: nginx runtime（已注释，独立单机部署时取消注释用）───────────
# FROM nginx:1.27-alpine AS runtime
#
# # Replace the default config with our SPA + reverse-proxy config
# COPY --from=build /app/dist /usr/share/nginx/html
# COPY apps/www/nginx.conf /etc/nginx/conf.d/default.conf
#
# # Healthcheck on the served index
# HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
#     CMD wget -q --spider http://127.0.0.1/ || exit 1
#
# EXPOSE 80
