# MEMPAS 前端 —— builder-only 镜像，配合共享 nginx 使用（不含 nginx 运行时）。
# 部署到跟 pixel-lora 共宿主的 ECS 时用这个（见 docs/DEPLOY.md §2.2）——
# 那台机器的 80/443 已经被 pixel-lora 的共享 nginx 占了，bid-compare 不能
# 再自带一个监听同一端口的 nginx 容器，只能把构建产物交给共享 nginx 读。
#
# apps/www/Dockerfile（保留，不删）还是单机独占部署时用的完整 nginx 运行时镜像。
#
# 用法： docker run --rm -v /opt/pixora/infra/nginx/html/mempas:/out <image>
FROM node:20-alpine AS build

WORKDIR /app

COPY apps/www/package.json apps/www/package-lock.json* ./
RUN npm config set registry https://registry.npmmirror.com && \
    npm ci --prefer-offline --no-audit --no-fund

COPY apps/www/ ./
RUN npm run build

CMD ["sh", "-c", "rm -rf /out/* && cp -r /app/dist/. /out/ && echo 'mempas-web dist copied to /out'"]
