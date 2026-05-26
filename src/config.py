import os

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
# Modo desktop (Electron): o renderer pode ter origin "null" (file://) ou
# http://localhost:<porta-vite>. Como a API só escuta em 127.0.0.1, liberar
# todas as origens é seguro nesse contexto.
#
# Em produção cloud use origens explícitas:
#   ALLOWED_ORIGINS=https://meusite.com,https://www.meusite.com
_raw = os.getenv(
    "ALLOWED_ORIGINS",
    "http://localhost:5173,http://localhost:5174,http://localhost:3000,null,*",
)
# Se a variável for literalmente "*", libera tudo; caso contrário, lista normal
if _raw.strip() == "*":
    ALLOWED_ORIGINS: list[str] = ["*"]
else:
    ALLOWED_ORIGINS = [o.strip() for o in _raw.split(",") if o.strip()]

# ---------------------------------------------------------------------------
# Servidor
# ---------------------------------------------------------------------------
PORT: int = int(os.getenv("PORT", "8000"))

# Em modo desktop a API deve escutar SOMENTE em localhost
HOST: str = os.getenv("HOST", "127.0.0.1")

# Optional HTTP proxy for stats.nba.com (blocked on cloud IPs without one)
# Ex: STATS_PROXY=http://user:pass@host:port
STATS_PROXY: str | None = os.getenv("STATS_PROXY") or None

# Live games worker
ENABLE_LIVE_WORKER: bool = os.getenv("ENABLE_LIVE_WORKER", "true").lower() == "true"
LIVE_POLL_INTERVAL_MS: int = int(os.getenv("LIVE_POLL_INTERVAL_MS", "2000"))

# ---------------------------------------------------------------------------
# Modo fixture (testar offline / sem jogos ao vivo)
# ---------------------------------------------------------------------------
# Setar USE_FIXTURES=1 faz o live_game_service ler de tests/fixtures/ em
# vez de bater na NBA Live API. Útil pra dev quando não tem jogo rolando.
USE_FIXTURES: bool = os.getenv("USE_FIXTURES", "0") == "1"

# ---------------------------------------------------------------------------
# Cache persistence
# ---------------------------------------------------------------------------
# Diretório onde o PersistentCache grava o JSON em disco.
#
# Default `/tmp` funciona em qualquer container, MAS é efêmero — todo
# redeploy/restart wipa. Em produção (Railway/etc), aponta pra um VOLUME
# persistente (ex: /data) pra que o cache sobreviva entre deploys.
#
# Como configurar no Railway:
#   1. Dashboard → seu serviço → Settings → Volumes → New Volume
#   2. Mount path: /data (~ 1GB é mais que suficiente)
#   3. Variables: CACHE_DIR=/data
#
# Sem volume persistente, todo deploy custa ~350 créditos ScraperAPI
# pra re-warmar o cache do zero. Com volume, custa zero (cache survive).
CACHE_DIR: str = os.getenv("CACHE_DIR", "/tmp")

# ---------------------------------------------------------------------------
# NBA Rotation integration (nbarotations.info)
# ---------------------------------------------------------------------------
# Feature flag pra ligar/desligar uso de rotação real no cálculo da
# projeção. Default ON — comportamento atual. Setar 0/false desativa
# o ajuste sem precisar redeploy de código (volta ao fallback uniforme).
ENABLE_NBA_ROTATION_ADJUSTMENT: bool = os.getenv(
    "ENABLE_NBA_ROTATION_ADJUSTMENT", "1"
).lower() in ("1", "true", "yes")

# Base URL da API/site. Permite trocar pra mirror em caso de mudança.
NBA_ROTATION_BASE_URL: str = os.getenv(
    "NBA_ROTATION_BASE_URL", "https://nbarotations.info"
)

# TTL do cache de perfis em segundos (default 7 dias). Padrões de rotação
# mudam devagar — não vale a pena refresh agressivo.
NBA_ROTATION_CACHE_TTL_SECONDS: int = int(
    os.getenv("NBA_ROTATION_CACHE_TTL_SECONDS", str(7 * 86_400))
)

# ---------------------------------------------------------------------------
# The Odds API — linhas reais de player props (PTS/REB/AST)
# ---------------------------------------------------------------------------
# Quando ENABLE_REAL_ODDS=1, o payload do hot ranking ganha `real_line`
# (média entre books US: DK, FanDuel, BetMGM, Bet365…) lado a lado com a
# linha synthetic. Usuário compara nossa projeção × mercado real.
#
# Cadência de cache calibrada pra economizar créditos:
#   not_started + > 5 min do tipoff  → não chama API
#   not_started + ≤ 5 min do tipoff  → 1× fetch (pré-aquece o cache)
#   Q1               → 120 s    (linhas movem pouco no início)
#   Q2, Q3 >5min     → 90 s
#   Q3 ≤5min, Q4, OT → 30 s     (crunch time)
#   final            → não chama API; devolve cache se existir, senão {}
#
# Plano $119 / 5M créditos atende ~3.6M créditos/mês em temporada cheia.
# Sem ODDS_API_KEY ou com ENABLE_REAL_ODDS=0, o serviço fica off — schema
# devolve real_line=None e o front renderiza só o synthetic.
ODDS_API_KEY: str | None = os.getenv("ODDS_API_KEY") or None
ENABLE_REAL_ODDS: bool = os.getenv("ENABLE_REAL_ODDS", "0").lower() in (
    "1", "true", "yes",
)
# Região de mercado (us | uk | eu | au). US tem mais books de player prop.
ODDS_REGIONS: str = os.getenv("ODDS_REGIONS", "us")
# Lista CSV de bookmakers específicos (ex: "draftkings,fanduel"). Vazio =
# todos os books do region — recomendado pra média robusta.
ODDS_BOOKMAKERS: str = os.getenv("ODDS_BOOKMAKERS", "")
