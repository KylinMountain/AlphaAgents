# Deployment

Runs the trading-day scheduler (`run-v2`) as a container. Everything is
keyed to the A-share calendar, so the container's TZ is fixed to
`Asia/Shanghai` regardless of the host.

## Layout on the box

```
~/docker/alphaagents/
├── repo/               git clone (build context)
├── data/               bind-mounted to /app/data — SQLite DBs + Chroma
└── .env                secrets, chmod 600, never committed or baked in
```

## First deploy

```bash
mkdir -p ~/docker/alphaagents && cd ~/docker/alphaagents
git clone https://github.com/KylinMountain/AlphaAgents.git repo
mkdir -p data

# Secrets — copy from a trusted machine, do not commit.
scp your-mac:~/path/to/.env ./.env && chmod 600 .env

cd repo && docker build -t alphaagents:local .
```

### Seed the data directory

`data/` starts empty. Three of the four stores are generated; the fourth
is faster to copy than to rebuild.

```bash
# stocks.db — concept/industry index (~5 min)
docker run --rm --env-file ../.env -v ~/docker/alphaagents/data:/app/data \
  alphaagents:local python main.py build-index

# chroma/ — semantic search vectors
docker run --rm --env-file ../.env -v ~/docker/alphaagents/data:/app/data \
  alphaagents:local python main.py build-embeddings
```

`market_history.db` (full-market daily K-lines) can be built with
`python main.py init-history`, but that walks ~5000 stocks through
baostock and takes hours. If a current copy exists elsewhere, transfer it
instead — the schema is a plain `daily_kline` table:

```bash
rsync -az --partial source-host:/path/to/market_history.db \
  ~/docker/alphaagents/data/market_history.db
```

`memory.db` is created on first run and accumulates themes, predictions
and hit rates. It is the one store with no rebuild path — back it up.

## Run

```bash
cd ~/docker/alphaagents/repo

docker compose up -d                    # scheduler only
docker compose --profile web up -d      # + pipeline UI on :8002
docker compose logs -f scheduler
```

The compose file reads `.env` from the repo directory, so either symlink
it (`ln -s ../.env .env`) or run compose with `--env-file ../.env`.

## Update

```bash
cd ~/docker/alphaagents/repo
git pull && docker compose build && docker compose up -d
```

## Ports

`8002` (web profile) → container `8000`. Host `8000`/`8001` belong to
TradingAgents-AShare; do not reuse them.

## Notes

- `mem_limit` is set on both services because the box runs alongside
  other containers. Raise it only if the host has headroom.
- Overseas news sources go through `CF_WORKER_URL` when set; without it
  those sources fail while the domestic ones keep working.
- Back up `data/memory.db` before any destructive operation.
