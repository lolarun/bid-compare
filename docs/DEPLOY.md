# MEMPAS Deployment Guide (Alibaba Cloud ECS · co-hosted with pixel-lora · GitHub Actions)

> **Status — 2026-08-25: shipped and verified.** Live at
> **<https://bid.hotcrp.cn/>** (HTTPS, not the originally planned HTTP-only —
> see "Three deviations from plan" below).
>
> First successful deployment: GitHub Actions run `32799699509`
> (2026-08-25 02:03 UTC). Before that the workflow **failed 9 times in a
> row** (never once succeeded since it was set up on 2026-08-18), all for
> the same root cause — none of the repository's GitHub Secrets were
> configured, so it stalled at `Username and password required` on ACR
> login. **Not a code problem, not an ECS environment problem.**
>
> Verification results (2026-08-25, measured in a real browser, not
> inferred):
>
> | Check | Result |
> |---|---|
> | Frontend <https://bid.hotcrp.cn/> | ✅ login page renders correctly |
> | Backend <https://bid.hotcrp.cn/api/health> | ✅ `{"status":"ok","service":"mempas","llm_provider":"dashscope_ocr"}` |
> | Image build + push to ACR | ✅ mempas-api / mempas-www both succeeded |
> | SSH deploy (container rebuild + dist extraction + nginx reload) | ✅ all completed |
>
> ### Three deviations from plan (all discovered by measurement, not design changes)
>
> 1. **Actually running on HTTPS, not the originally planned HTTP-only.**
>    The original plan said "HTTP-only for now, port 80, 443 later" —
>    measurement showed **port 80 is blocked by the Alibaba Cloud gateway**
>    (`curl -I http://bid.hotcrp.cn` returns 403 with response header
>    `Server: Beaver`, which is Alibaba Cloud's blocking gateway, not nginx;
>    connecting directly to `106.14.113.209` gives the same 403, meaning the
>    request never reaches the server). This is the typical port-80 block
>    for a domain without ICP filing. **Port 443 works fine**, so the actual
>    live address is `https://`. §3.4's description of HTTP-only no longer
>    holds.
> 2. **`deploy.sh`'s health check can report a false failure.** The log line
>    `⚠️ Health check did not pass, check docker compose logs backend` can
>    appear even though the service is completely healthy. The cause is
>    that the script only `sleep 5`s after the container reports `Started`
>    before probing — the backend isn't finished starting yet. **This line
>    uses `|| echo`, so it never fails the deployment** — it's noise, not a
>    real failure — but that also means **a genuine failure won't turn the
>    workflow red either**; seeing this warning is not something you can
>    treat as "fine," it needs manual follow-up.
> 3. **`ECS_SSH_KEY` reuses pixel-lora's deploy key**:
>    `pixel-lora/infra/ssh/pixora_deploy` (not `~/.ssh/id_rsa`). That key
>    was already provisioned for `106.14.113.209`; the two projects share
>    the host, so it's reused directly.
>
> ### GitHub Secrets (the one and only prerequisite — all five are required)
>
> Configure these under **bid-compare's own repository** Settings → Secrets.
> **GitHub Secrets are isolated per repository** — having them configured on
> pixel-lora does not make them usable here, and there is no cross-repo
> read access. This is exactly what caused the 9 earlier failures.
>
> | Secret | Where the value comes from |
> |---|---|
> | `ACR_REGISTRY` | `pixora-acr-registry.cn-shanghai.cr.aliyuncs.com` (same instance as pixel-lora) |
> | `ACR_USERNAME` | The access-credential username for Alibaba Cloud Container Registry |
> | `ACR_PASSWORD` | The matching password. **Not viewable in the console, only resettable**; resetting it invalidates pixel-lora's identically-named secret too, so both repos must be updated together |
> | `ECS_HOST` | `106.14.113.209` |
> | `ECS_SSH_KEY` | The contents (PEM) of `pixel-lora/infra/ssh/pixora_deploy` |
>
> ```bash
> gh secret set ACR_REGISTRY --repo lolarun/bid-compare --body "pixora-acr-registry.cn-shanghai.cr.aliyuncs.com"
> gh secret set ECS_HOST     --repo lolarun/bid-compare --body "106.14.113.209"
> gh secret set ECS_SSH_KEY  --repo lolarun/bid-compare < ../pixel-lora/infra/ssh/pixora_deploy
> gh secret set ACR_USERNAME --repo lolarun/bid-compare   # paste interactively
> gh secret set ACR_PASSWORD --repo lolarun/bid-compare   # paste interactively
> gh secret list --repo lolarun/bid-compare               # confirm all 5 are present
> ```
>
> ### Routine releases
>
> Triggered automatically on push to `main`; can also be run manually:
>
> ```bash
> gh workflow run build-and-deploy --repo lolarun/bid-compare
> gh run list --repo lolarun/bid-compare --limit 1
> ```
>
> After releasing, **verify it yourself** (the health check can false-report,
> see deviation ②):
>
> ```bash
> curl -s https://bid.hotcrp.cn/api/health     # expect {"status":"ok",...}
> ```
>
> ### `.env` on the server (not version-controlled — a separate thing from GitHub Secrets)
>
> GitHub Secrets only cover **building and pushing images**; the ECS runtime
> needs two more files:
>
> - `/opt/mempas/.env` — `ACR_REGISTRY` / `TAG`, used by
>   `docker-compose.prod.yml` to assemble the image reference (`deploy.sh`
>   line 25, `source .env`)
> - `/opt/mempas/apps/api/.env` — backend runtime secrets
>   (`DASHSCOPE_API_KEY`, `BAIDU_UNLIMITED_OCR_*`, etc.).
>
>   **`MIMO_API_KEY` — required as of 2026-08-27, and this entry's earlier
>   description ("optional; only enables design/41's page filter") is now
>   wrong on both counts.** It is the credential for the *default* text and
>   vision vendor: `domain_config.TEXT_CLIENT_VENDOR` and
>   `VISION_CLIENT_VENDOR` both default to `'mimo'`. Unset, every migrated
>   call site falls back to DashScope and logs a warning — safe, but
>   invisible: the deployment runs the old vendor and only a log line says
>   so. Either set it, or change those two switches back to `'dashscope'`
>   deliberately.
>
>   It no longer controls the page filter. That is a separate product
>   decision behind its own switch, `domain_config.PAGE_FILTER_ENABLED`
>   (default `False`, 2026-08-28) — previously the two shared this one
>   variable, so setting the key would have silently switched the filter on
>   as well.
>
> The appendix below is the **legacy single-machine plan** (a standalone
> ECS instance at `101.37.166.68`, manual `git pull && docker compose up -d
> --build`). That machine has been decommissioned and is unreachable; the
> content is kept for historical reference and rollback context only —
> **it is not the current instructions.**



## Table of contents

- [1. Migration background and decisions](#1-migration-background-and-decisions)
- [2. Target architecture](#2-target-architecture)
- [3. Files to create/modify](#3-files-to-createmodify)
- [4. Migration steps (first-time cutover)](#4-migration-steps-first-time-cutover)
- [5. Routine updates (post-migration)](#5-routine-updates-post-migration)
- [6. Operations (backup/monitoring/rollback)](#6-operations-backupmonitoringrollback)
- [Appendix: legacy single-machine plan (deprecated, historical reference only)](#appendix-legacy-single-machine-plan-deprecated-historical-reference-only)

---

## 1. Migration background and decisions

| Item | Old plan | New plan |
|---|---|---|
| ECS | A standalone instance, `101.37.166.68` (**deprecated, unreachable, no longer maintained**) | Reuse pixel-lora's `106.14.113.209` |
| Deploy path | `/opt/mempas` on the ECS; `git pull` then local `docker compose up -d --build` | `/opt/mempas` on the ECS (directory name unchanged), just `docker compose pull` + `up -d` — no build runs on the ECS |
| Image registry | None (local build, no registry involved) | Reuse pixel-lora's existing Alibaba Cloud ACR instance, new namespace `bidcom` (not pixel-lora's `pixora`, so images never mix) |
| CI/CD | None (pure manual SSH) | GitHub Actions: push to main → build → push to ACR → SSH-triggered ECS deploy, structure copied from pixel-lora's `deploy.yml` |
| Reverse proxy | bid-compare's own nginx container solely owns 80/443 | **Plugs into pixel-lora's existing shared nginx container**, adding one server block (bid-compare no longer ships its own nginx container listening on 80/443 externally, see §2.2) — this is the **only** place that touches the pixel-lora repository, and only once; every release after that doesn't touch it again |
| Database | SQLite, volume-mounted at `/opt/mempas/data/` | Unchanged — neither project uses MySQL/RDS; SQLite data stays fully independent of pixel-lora's, physically on the same host but sharing no data |

**Why reuse the ACR instance instead of opening a new one**: pixel-lora is
already paying to maintain an ACR instance; adding one more namespace is
near-zero marginal cost, whereas a new instance would be a duplicate fixed
monthly fee. The risk is that the two projects' image cleanup
policy/quota is shared — §3.3 handles that.

**Why reuse the same ECS instead of a new one**: bid-compare is an internal
tool (an estimated ≤50 online users, 5–10 OCR calls/minute), needing far
less than the 4C8G originally provisioned for it standalone. pixel-lora's
ECS is 8C16G, and inference/training all run on PAI-EAS/PAI-DLC, so the ECS
itself only carries nginx+API+two lightweight workers — in theory there's
headroom. **But that's a theoretical estimate that had to be confirmed on
the real machine before migrating** (see §4 step 1), not assumed.

**How independent the two projects actually are** (confirmed 2026-08-18):
the image registry (ACR instance) and the physical host (ECS) are shared;
everything else is fully independent — separate GitHub repositories,
separate Actions workflows, separate `docker compose` projects
(`/opt/pixora` vs `/opt/mempas`), separate databases, with neither aware of
the other's release cadence. **The one exception, and the only place that
needs to touch the pixel-lora repository**: since the host's 80/443 ports
can only be listened on by one process, and pixel-lora's nginx container
currently holds them, getting `bid.hotcrp.cn` onto the standard port 80
requires adding one routing rule inside that shared nginx. This is a
**one-time, minimal change** (the server block given in §3.4, added once
and done) — after that, every subsequent bid-compare release (code change,
new feature) only triggers its own repo's GitHub Actions and never touches
the pixel-lora repository again.

## 2. Target architecture

### 2.1 Overall topology

```
                     ECS 106.14.113.209 (8C16G, co-hosted with pixel-lora)
                     ┌──────────────────────────────────────────────┐
Internet ── 80/443 ─►│  Shared nginx (pixel-lora's existing container,│
                     │  this round only adds configuration)          │
                     │    ├─ server: aiguozhanbijin.com.cn      → pixora-www dist
                     │    ├─ server: mng.aiguozhanbijin.com.cn  → pixora-mng dist
                     │    ├─ server: api.aiguozhanbijin.com.cn  → pixora-api:8000
                     │    ├─ server: m.aiguozhanbijin.com.cn    → pixora-h5 dist
                     │    └─ server: bid.hotcrp.cn (HTTPS/443)   → mempas-www dist
                     │                                    │
                     │                          proxy /api/ ▼
                     │                          172.18.0.1:8100 (bridge-gateway-only)
                     │                                    │
                     │              /opt/mempas/docker-compose.prod.yml
                     │              ┌──────────────────────────────┐
                     │              │  backend  (mempas-api)       │
                     │              │  publishes 172.18.0.1:8100→8000│
                     │              │  volume: /opt/mempas/data     │
                     │              └──────────────────────────────┘
                     └──────────────────────────────────────────────┘
```

**Key design decision (confirmed 2026-08-18 with real ECS data, no longer a
guess)**: measurement showed pixel-lora's shared nginx container (real name
`infra-nginx-1`, not the earlier guess `pixora-nginx-1` — the compose
project name comes from the directory name `infra/`) runs on the standard
bridge network `infra_default`, gateway `172.18.0.1` (confirmed via
`docker network inspect infra_default`; not a fixed value, see "known
fragility" below). `127.0.0.1` inside a container points at the container
itself, not the host — the initially imagined "bind to 127.0.0.1, talk over
the loopback" route **does not work**, and has been ruled out by testing.

Instead: bid-compare's backend container **binds to `172.18.0.1:8100`**
(the docker bridge gateway address, not `0.0.0.0`). Why that instead of
`0.0.0.0:8100`: the ECS's ufw only allows 22/80/443/2222 (confirmed via
`ufw status`), but Docker's own forwarding rules (the `DOCKER-FORWARD`/
`DOCKER-USER` chains) are, by default, not subject to ufw's INPUT chain —
a well-known docker+ufw gotcha. Whether `0.0.0.0:8100` is actually blocked
from the public internet **cannot be concluded from ufw's output alone**.
Binding to the gateway IP instead of `0.0.0.0` rules out public
reachability at the listen-address level itself, independent of whether
the ufw/iptables rules happen to be correct — simpler and safer.

Compared with "join the mempas container to pixora's docker network":
binding to the gateway IP means bid-compare's compose file never needs to
know pixel-lora's network name, which is lower coupling. **Known
fragility**: if pixel-lora ever does a full `docker compose down` (not just
`up -d`) that rebuilds the `infra_default` network, the gateway IP could in
theory change (in practice, this is the only custom network on the host and
nothing else contends for the subnet, so it almost always lands on the same
one — but that's not a 100% guarantee). If it does change, the symptom is
`bid.hotcrp.cn`'s `/api/` returning 502; fixing it is a one-line IP change
in nginx.conf — small blast radius, easy to diagnose — a better trade-off
than betting on ufw/iptables being configured correctly for `0.0.0.0`, or
coupling the two projects' docker networks together.

### 2.2 Why bid-compare can't keep its own nginx container

`docker-compose.yml` (the standalone single-machine plan) publishes the
frontend service on `80:80`, which is fine when **owning an ECS
exclusively**; once co-hosted, 80/443 are already held by pixel-lora's
nginx, and two nginx containers can't both listen on the same host port.

The approach: mirror pixel-lora's `www.Dockerfile`/`mng.Dockerfile`
pattern — the frontend image is only a "builder"; its `CMD` copies `dist/`
into a mounted `/out` (a host directory), and it no longer ships an nginx
runtime of its own. Static files are served by the **shared nginx**.

**Decision on 2026-08-20**: rather than maintaining a separate
`Dockerfile.builder` file, `apps/www/Dockerfile` itself was modified — the
former "Stage 2: nginx runtime" is commented out (not deleted), so the
default build product is now a builder-only image. Whenever MEMPAS leaves
the co-hosted setup and gets its own ECS again, uncommenting restores the
old behavior. **Cost**: after this change, `docker-compose.yml` (the
standalone plan, see appendix) **no longer works** — it expects this
Dockerfile to produce a container that stays up listening on port 80, but
the default product is now a one-shot builder that exits once the build is
done; the two are incompatible. Switching back to the standalone plan
requires uncommenting Stage 2 first.

### 2.3 Namespace and images

| bid-compare image | ACR path |
|---|---|
| Backend | `<ACR_REGISTRY>/bidcom/mempas-api:latest` / `:<sha>` |
| Frontend (builder, produces dist) | `<ACR_REGISTRY>/bidcom/mempas-www:latest` / `:<sha>` |

`<ACR_REGISTRY>` reuses the same instance address pixel-lora uses (a VPC
internal-domain version is used by the ECS to pull images, a public
version by GitHub Actions to push them — the actual values for both need
to be taken from pixel-lora's GitHub Secret `ACR_REGISTRY`; this side can't
see the actual value, only how the workflow references it).

## 3. Files to create/modify

### 3.1 `.github/workflows/deploy.yml` (new, in the bid-compare repository)

Structure copied from pixel-lora's `deploy.yml`, with its 5-image
path-filter matrix removed (bid-compare only has 2 images, no need for
that complexity) — simplified to a fixed two-image build:

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
            dockerfile: apps/www/Dockerfile   # Stage 2 commented out, default product is a builder-only image, see §2.2
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

**Required GitHub Secrets** (in bid-compare's own repository Settings →
Secrets — not reused from pixel-lora's repository secrets; the two repos
are configured separately, even though the values can be identical):

| Secret | Description | Can it be copied directly from pixel-lora's value? |
|---|---|---|
| `ACR_REGISTRY` | Same ACR instance address | ✅ copy directly |
| `ACR_USERNAME` / `ACR_PASSWORD` | ACR access credentials | ✅ copy directly (the credential is instance-level; if namespace-level RBAC is enabled, separately confirm the `bidcom` namespace has push permission) |
| `ECS_HOST` | `106.14.113.209` | ✅ |
| `ECS_SSH_KEY` | SSH private key | ⚠️ can reuse pixel-lora's `infra/ssh/pixora_deploy`, or a dedicated bid-compare deploy key can be issued (smaller permission surface, recommended but not required) |

### 3.2 `apps/www/Dockerfile` (modified, no separate file created)

Decision on 2026-08-20: rather than maintaining a separate
`Dockerfile.builder`, the existing `apps/www/Dockerfile` was modified
directly — the entire "Stage 2: nginx runtime" section is commented out
(kept in the file, not deleted), so `docker build -f apps/www/Dockerfile .`
now produces a builder-only image by default (Stage 1 gained one `CMD`
line that copies `dist/` into the mounted `/out`):

```dockerfile
FROM node:20-alpine AS build
WORKDIR /app
COPY apps/www/package.json apps/www/package-lock.json* ./
RUN npm config set registry https://registry.npmmirror.com && \
    npm ci --prefer-offline --no-audit --no-fund
COPY apps/www/ ./
RUN npm run build
CMD ["sh", "-c", "rm -rf /out/* && cp -r /app/dist/. /out/ && echo 'mempas-www dist copied to /out'"]

# ─── Stage 2: nginx runtime (commented out; uncomment for standalone single-machine deploys) ───
# FROM nginx:1.27-alpine AS runtime
# COPY --from=build /app/dist /usr/share/nginx/html
# COPY apps/www/nginx.conf /etc/nginx/conf.d/default.conf
# HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
#     CMD wget -q --spider http://127.0.0.1/ || exit 1
# EXPOSE 80
```

**Cost** (already mentioned once in §2.2): `docker-compose.yml` (the
standalone single-machine plan, see appendix) **no longer works** — it
expects this file to produce a container that stays up listening on port
80; the default product now is a one-shot builder that exits once the
build is done. Switching back to the standalone plan requires uncommenting
Stage 2 first.

### 3.3 `docker-compose.prod.yml` (already built, in the bid-compare repository root)

> What follows is the version from when it was **first written**, kept to
> explain the design intent; the actual file has since gained a
> `reservations` resource reservation and updated comments. As with §4
> step 6, **the actual `docker-compose.prod.yml` in the repository is
> authoritative when it differs from this** — this is not a canonical copy.

```yaml
# Pulls a pre-built image rather than building on the ECS — pairs with the
# image GitHub Actions produces. Coexists with the existing docker-compose.yml
# (local-build version), neither interferes with the other; scripts/deploy.sh
# (created in §4) uses this file.
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
      # Bind to the gateway address of the bridge network pixel-lora's shared
      # nginx lives on — not 0.0.0.0. This rules out public reachability at the
      # listen-address level itself, independent of whether ufw/iptables rules
      # are correct (measured value, see §2.1; if 172.18.0.1 is no longer
      # infra_default's gateway, re-confirm with
      # `docker network inspect infra_default` and update this).
      - "172.18.0.1:8100:8000"
    deploy:
      resources:
        limits:
          cpus: "1.5"
          memory: 2.5G
```

The `frontend` service is no longer needed — static files are extracted by
`scripts/deploy.sh` (§4) into the shared nginx's mounted directory using a
one-shot container, not a long-running service.

### 3.4 Access domain — `bid.hotcrp.cn`, actually running on HTTPS (the original text of this section is stale)

> **Corrected 2026-08-25: the "SSL not configured for now, running on port
> 80 HTTP" statement below no longer holds.** Measurement showed port 80 is
> blocked by the Alibaba Cloud gateway (403 + `Server: Beaver`; connecting
> directly to the IP gives the same result, meaning the request never
> reaches the server), while **443 works fine**. The live production
> address is **<https://bid.hotcrp.cn/>**. The rest of this section is kept
> for historical reference; treat the status banner at the top of this
> document as authoritative when configuring anything.

### 3.4 (original text, now stale) Access domain — decided: `bid.hotcrp.cn`, HTTP-only for now

Confirmed 2026-08-18: the domain is `bid.hotcrp.cn`, **no SSL for now, running
on port 80 HTTP** (not `.com.cn`, so this is not a subdomain of pixel-lora's
`aiguozhanbijin.com.cn` — it's a fully independent domain; DNS resolution
and ICP filing need to be confirmed separately, and cannot borrow
pixel-lora's already-filed identity).

**Outstanding items** (not blocking the current deployment, recorded here
so they aren't forgotten):
- Confirm `bid.hotcrp.cn`'s DNS A record points at `106.14.113.209` (must
  be done before migrating, otherwise the domain won't resolve to
  anything).
- Confirm whether this domain, accessed over the public internet via a
  mainland-China ECS, needs its own ICP filing (`docs/contract/ICP备案材料
  及流程.md` has the materials checklist — this follows the new-domain
  process, not the shared-subdomain-filing process). HTTP-only does not
  mean filing isn't required — the filing requirement has nothing to do
  with whether SSL is configured; it's triggered by "domain resolves to a
  mainland server + is publicly reachable."
- Add SSL later: certificate request + adding a 443 server block to nginx +
  `location / { return 301 https://$host$request_uri; }` redirect, following
  the pattern used in pixel-lora's `nginx.conf` desktop server block at
  that time (already read when this document was originally written).

The corresponding shared-nginx server block (for use when §4 step 4 is
carried out):

```nginx
server {
    listen 80;
    server_name bid.hotcrp.cn;

    client_max_body_size 100M;   # OCR uploads (same limit as the existing apps/www/nginx.conf)

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

## 4. Migration steps (first-time cutover)

> Steps 1–2 have been confirmed with real ECS data (results already written
> into §2.1, marked ✅ below).

1. ✅ **ECS resource headroom**: measured 8 vCPU / 14GB available; the 5
   existing pixel-lora containers together use <1% CPU and about 1GB
   memory, so adding bid-compare's estimated 1.5C/2.5G stays comfortably
   within headroom. Disk: 118G total, 73G used, 41G available.
2. ✅ **Network reachability**: the §2.1 conclusion has been confirmed by
   measurement — `infra_default`'s gateway is `172.18.0.1`; the chosen
   approach is binding to the gateway IP (not the loopback-address
   approach, which has been proven not to work).
3. **DNS**: confirm `bid.hotcrp.cn`'s A record points at `106.14.113.209`
   (§3.4's first outstanding item) — **this step has to be done at the
   domain registrar; there's no visibility into the DNS console from
   here.**
4. **[One-time] In the pixel-lora repository**, add/modify (this is the
   only place that touches that repository, see §1 "How independent the
   two projects actually are"; after this one change, every subsequent
   bid-compare release never touches the pixel-lora repository again):
   - `infra/nginx/nginx.conf`: add the `bid.hotcrp.cn` server block given
     in §3.4 (`proxy_pass http://172.18.0.1:8100/api/;`, already using the
     measured gateway IP, no further changes needed). Also needs one line
     added to the nginx service's `volumes` in `docker-compose.prod.yml`:
     `./nginx/html/mempas:/usr/share/nginx/html/mempas:ro`.
   - `scripts/deploy.sh`: add a "pull the mempas-www builder image →
     extract dist → reload nginx" section, mirroring what's done for
     www/mng/h5 — though this logic more properly belongs in
     **bid-compare's own** `scripts/deploy.sh` (created in §4 step 6),
     since mempas's release cadence is independent of pixora's and
     shouldn't be coupled into pixora's deploy script trigger.
     **Recommendation**: bid-compare's own deploy.sh extracts dist into
     `/opt/pixora/infra/nginx/html/mempas` (writing into pixora's
     directory) and `docker exec`s pixora's nginx container to reload —
     that way the two sides' deployments trigger independently of each
     other, sharing only the one piece of configuration for "which
     directory nginx reads."
5. Configure the 4 secrets from the §3.1 table in the GitHub repository's
   Settings.
6. **`scripts/deploy.sh`** (bid-compare repository; already built and
   working — the following is its current actual content, cross-checked
   for consistency on 2026-08-27 — **this code block will drift; when it
   differs from the file in the repository, the file is authoritative, not
   this copy**):

   ```bash
   #!/usr/bin/env bash
   set -euo pipefail
   cd /opt/mempas
   set -a; source .env; set +a   # ACR_REGISTRY / TAG

   echo "=== pull backend image ==="
   docker compose -f docker-compose.prod.yml pull backend

   echo "=== restart backend (includes automatic migration, see note below) ==="
   docker compose -f docker-compose.prod.yml up -d --remove-orphans

   echo "=== wait api healthy ==="
   sleep 5
   curl -sf http://172.18.0.1:8100/api/health || echo "⚠️  Health check did not pass, check docker compose logs backend"

   echo "=== pull + extract frontend dist ==="
   docker pull "${ACR_REGISTRY}/bidcom/mempas-www:${TAG:-latest}"
   mkdir -p /opt/pixora/infra/nginx/html/mempas
   docker run --rm -v /opt/pixora/infra/nginx/html/mempas:/out \
     "${ACR_REGISTRY}/bidcom/mempas-www:${TAG:-latest}"

   echo "=== reload shared nginx (pixel-lora's infra-nginx-1 container) ==="
   docker exec infra-nginx-1 nginx -s reload

   echo "=== prune dangling images ==="
   docker image prune -f
   ```

   **There is no separate `alembic upgrade head` step — this is
   intentional, not an omission.** MEMPAS's migrations run automatically
   inside `apps/api/core/database.py::init_db()` at FastAPI startup
   (Alembic is configured programmatically, with no dependency on
   `alembic.ini`), so they run as a side effect whenever
   `docker compose up -d` restarts the backend; the image doesn't even
   copy in `alembic.ini`, so running the `alembic upgrade head` CLI command
   on its own would fail immediately with `FileNotFoundError`. This is
   MEMPAS's own existing design, different from pixel-lora's "explicit
   one-shot migration" pattern — copying pixel-lora's deploy.sh structure
   here would not work.

   The container name `infra-nginx-1` has been confirmed on the ECS with
   `docker ps` — it's a real, measured value (the compose project name
   comes from the directory `infra/`, not the repository name `pixora`),
   not a guess.
7. **Run through the entire flow manually once** (not via GitHub Actions —
   execute every step by hand, locally/on the ECS) to confirm it works
   before letting GitHub Actions take over.
8. Once everything is verified, **decommission** `101.37.166.68` (release
   the instance if it's still being billed).

## 5. Routine updates (post-migration)

Same as pixel-lora: pushing to the `main` branch automatically builds and
deploys — no more manual SSH needed:

```bash
git push origin main
# GitHub Actions automatically: build → push to ACR → SSH-trigger the ECS to run scripts/deploy.sh
```

Manually forcing a redeploy (e.g. only `.env` changed, no code change):
trigger `workflow_dispatch` manually from the GitHub repository's Actions
page, or SSH in directly and run `bash /opt/mempas/scripts/deploy.sh`.

## 6. Operations (backup/monitoring/rollback)

The following carries over unchanged from the old plan, same paths
(`/opt/mempas/data/`):

- **Backup**: cron `0 2 * * * cd /opt/mempas && cp data/mempas.db data/mempas-$(date +\%F).db.bak`, see the appendix original.
- **Monitoring**: `GET /api/health`, `GET /api/health/queue`, thresholds unchanged.
- **Rollback**:
  - Code: `git checkout <sha> && bash scripts/deploy.sh`, provided that
    sha's image hasn't been cleaned up from ACR yet — the ACR retention
    policy needs to be cross-checked with pixel-lora, since sharing the
    instance means sharing the cleanup policy; it can't be set
    independently for this project alone.
  - Database: `cp data/mempas-<date>.db.bak data/mempas.db`, unchanged from
    the old plan.

---

## Appendix: legacy single-machine plan (deprecated, historical reference only)

> What follows is the original content from the 2026-05 version.
> **`101.37.166.68` has been decommissioned and is no longer maintained** —
> this is kept only for historical reference, and for reference if the
> shared-ECS plan ever needs to be rolled back to a standalone machine. The
> ECS sizing cost figures and the SQLite operational experience (WAL /
> backup / upgrade triggers) are still valid; only "which machine to deploy
> to" and "how to build the image" have been superseded by §1–6 above.

### Original §0. Routine quick update (deprecated)

```bash
ssh root@101.37.166.68 "cd /opt/mempas && git pull && docker compose up -d --build"
```

### Original §1. ECS sizing (budget optimization — figures still have reference value)

| User count | ECS spec | Monthly cost (East China 1, pay-as-you-go/annual) | Notes |
|---|---|---|---|
| ≤ 20 online / 1–3 OCR·min⁻¹ | `ecs.g7.xlarge` 4 vCPU / 8 GB | ~¥230/mo annual | Starting spec for a standalone deployment |
| 30–50 online / 5–10 OCR·min⁻¹ | `ecs.g7.2xlarge` 8 vCPU / 16 GB | ~¥460/mo annual | Upgrade when multiple suppliers upload OCR simultaneously |

Disk: 50 GB ESSD, ¥18/month. Bandwidth: 5 Mbps pay-as-you-go, ~¥30/month.

### Original §3. Directory structure (runtime — still applicable after co-hosting)

```
/opt/mempas/
├── apps/api/.env           ← secrets (gitignored, must be manually chmod 600)
├── data/                   ← persistent volume (bound to the backend container's /app/data)
│   ├── mempas.db           ← SQLite main database
│   ├── mempas.db-wal       ← SQLite WAL
│   └── uploads/2026XXXX/   ← OCR upload files (organized by date)
└── docker-compose.yml / docker-compose.prod.yml
```

**Critical**: never commit `data/`, always back it up.

### Original §6. Capacity and upgrade path (criteria still applicable)

Monitor `GET /api/health/queue`:

| Symptom | Action |
|---|---|
| `queue_depth = 0` sustained | everything normal |
| `queue_depth = 1–3` occasional | normal fluctuation |
| `queue_depth > max_workers` sustained for 5+ minutes | upgrade path (add arq + Redis, same ECS) |
| backend container CPU > 70% sustained | discuss an overall ECS spec upgrade with pixel-lora, don't upgrade just bid-compare's slice alone |

### Original §8. Common issues (unchanged)

**Q: `llm_provider` shows `mock`?**
A: The API key didn't load. Check that `apps/api/.env` exists and contains `DASHSCOPE_API_KEY=sk-xxx`.

**Q: SQLite reports `database is locked`?**
A: WAL is already on (`PRAGMA journal_mode=WAL`). If it still happens, most
likely some external process is writing to the DB (a backup script?), or
this has genuinely hit SQLite's write-concurrency limit.
