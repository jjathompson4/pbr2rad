# Deploying pbr2rad.com (Fly.io + Cloudflare)

The web app ships as a Docker container (Python + official Radiance 6.0
binaries, so `/preview` works). It runs on Fly.io with scale-to-zero;
Cloudflare fronts the domain for TLS, caching, and WAF.

## One-time setup

### 1. Install flyctl and sign in

```bash
brew install flyctl
```

```bash
fly auth login
```

### 2. Create the app and deploy

From the repo root (Dockerfile + fly.toml are already there):

```bash
fly launch --copy-config --no-deploy
```

Accept the existing config when prompted. If the name `pbr2rad` is taken,
pick another (e.g. `pbr2rad-web`) — the public domain will be
pbr2rad.com either way. Then:

```bash
fly deploy
```

First build takes a few minutes (downloads Radiance + pip installs).
Verify at the `*.fly.dev` URL it prints — check `/` and `/api/v1/health`.

### 3. Allocate IPs and request certs

```bash
fly ips allocate-v4 --shared
```

```bash
fly ips allocate-v6
```

```bash
fly certs add pbr2rad.com
```

```bash
fly certs add www.pbr2rad.com
```

`fly certs add` prints DNS records to create, including an
`_acme-challenge` CNAME used for issuance. Note the IPs from
`fly ips list`.

### 4. Cloudflare DNS (dash.cloudflare.com → pbr2rad.com → DNS)

| Type  | Name              | Content                          | Proxy    |
|-------|-------------------|----------------------------------|----------|
| A     | `@`               | Fly IPv4 from `fly ips list`     | Proxied  |
| AAAA  | `@`               | Fly IPv6 from `fly ips list`     | Proxied  |
| CNAME | `www`             | `pbr2rad.com`                    | Proxied  |
| CNAME | `_acme-challenge` | value printed by `fly certs add` | DNS only |

Then under **SSL/TLS**, set the mode to **Full (strict)** — with the
default "Flexible" mode Cloudflare speaks plain HTTP to Fly and you get
redirect loops.

Wait for issuance to finish:

```bash
fly certs check pbr2rad.com
```

### 5. Smoke test

Open https://pbr2rad.com — the UI should load, a Poly Haven fetch and an
ambientCG fetch (Browse tab → source toggle) should each round-trip through
convert, and `/api/v1/health` should return OK.

## Notes

- **Scale to zero**: the machine stops when idle; the first request after
  a quiet period takes a few seconds to cold-start. `min_machines_running = 1`
  in fly.toml removes that at the cost of ~$6/mo.
- **Rate limiting**: the app's per-IP limiter works behind the proxies
  because uvicorn runs with `--proxy-headers` (see Dockerfile CMD). For a
  second layer, Cloudflare → Security → WAF rate-limiting rules.
- **Upload cap**: the API caps requests at 80 MB, under Cloudflare's
  100 MB proxied-request limit on the free plan.
- **Single instance assumption**: the in-memory rate limiter and the
  per-source catalog caches (Poly Haven, ambientCG) are per-instance.
  Keep `count = 1` (the default) unless those move to a shared store.
- **Redeploys**: just `fly deploy` after changes. The per-source disk
  caches (`~/.cache/pbr2rad/<source>/`: downloaded maps/zips and the
  catalog JSON) are ephemeral and reset on redeploy; they repopulate on
  use. The ambientCG catalog is ~5 paged API calls (~15 MB, ~12 s cold);
  `fly.toml` sets `PBR2RAD_PREFETCH_CATALOGS = "1"` so the app warms it (and
  Poly Haven's) in a background thread at boot instead of on the first
  search. Catalogs are cached 6 h (ambientCG) / 1 h (Poly Haven) and served
  stale if the upstream API is down.
- **ambientCG map thumbnails**: the first `/info` for an ambientCG asset
  downloads its 1K-JPG pack (4–10 MB) into the cache to build per-map
  thumbnails; the later convert reuses it. These downloads are throttled
  process-wide (burst 30, then 3/min, ≤ 2 concurrent) and skipped once the
  cache exceeds 2 GB, so browsing can't turn the site into a bulk
  downloader.
- **Rootfs + cache**: the machine's rootfs is ephemeral (~8 GB, slow) and
  with Fly's default is *reset on every auto-stop*. `fly.toml` sets
  `persist_rootfs = "always"` so the download cache and catalogs survive
  stop/start and deploys; `PBR2RAD_CACHE_MAX_MB = "1024"` caps the cache
  with least-recently-modified eviction (catalogs exempt), enforced at
  boot, every 10 min and after each download. Stopped-rootfs storage is
  billed per GB, so the cap also bounds that cost. Temp job dirs are still
  swept after 30 min.
