# WNBA Analysis API — fork do NBA Analysis API

> ⚠️ **FORK EXPERIMENTAL (WNBA).** É uma cópia do `nba-analysis-api`
> adaptada para a WNBA, pra validar localmente. Reaproveita ~todo o
> código; o que difere por liga foi centralizado em **`src/league.py`**.
>
> ### O que muda da NBA pra cá
> - `src/league.py`: `LEAGUE_ID=10` (WNBA), jogo de **40 min** (4×10),
>   temporada `YYYY` (mai–out), sport key `basketball_wnba`, headshot CDN WNBA.
> - `nba_service.search_players`: usa `CommonAllPlayers(LeagueID=10)` (a
>   lista estática do nba_api é só NBA).
> - `gamelog`, `hot-board`, `team-board`: passam `LeagueID=10`.
> - `.env`: `LEAGUE_ID=10`, `DEFAULT_SEASON=2025`, **`ENABLE_LIVE_WORKER=false`**
>   e **`ENABLE_NBA_ROTATION_ADJUSTMENT=0`** (ver abaixo), `PORT=8001`.
>
> ### O que FUNCIONA hoje (camada de stats/histórico)
> busca de jogadoras, gamelog, análise de temporada, stats por jogo,
> pontos por período, hot-board e team-board — tudo via `LeagueID=10`.
>
> ### O que está PENDENTE (NBA-coupled)
> - **Ao vivo**: o feed atual é `cdn.nba.com` (NBA-only). WNBA precisa de
>   fonte ao vivo própria → live worker **desligado**.
> - **Rotação**: `nbarotations.info` é só NBA (sem equivalente WNBA) →
>   ajuste **desligado**; projeção cai no fallback de minutos.
> - **Modelo de 40 min** na rotação/projeção ao vivo: só importa quando
>   houver dado ao vivo; por ora não roda.
>
> ### ⚠️ Para TESTAR DADOS reais localmente: precisa de proxy
> Do seu IP residencial o `stats.nba.com` dá timeout (vale pra NBA e WNBA
> igual — é o motivo do `STATS_PROXY` em produção). Pra puxar dados WNBA
> local, set `STATS_PROXY=http://user:pass@host:port` no `.env` (a mesma
> credencial usada no Railway). Sem proxy, a API **sobe** e `/health`
> responde, mas as rotas de dados vão dar timeout.
>
> ### Rodar
> ```bash
> python -m venv .venv && source .venv/Scripts/activate
> pip install -r requirements.txt
> python run.py            # sobe em http://localhost:8001 (lê o .env)
> ```

---

Backend de análise de estatísticas da NBA que alimenta o **ClutchPro**.
Cruza forma recente, projeção fim-de-jogo, rotação histórica, linhas
sintéticas (e reais, opcionais) para leitura de jogo em tempo real.

> **Posicionamento:** o produto **não recomenda aposta**. A API entrega
> *forma, projeção, Clutch Score, heat, contexto e linha sintética* — a
> interpretação fica com o usuário.

---

## Stack

- **Python 3.12** + **FastAPI 0.115** (`uvicorn[standard]`)
- **Pydantic 2** — contratos de entrada/saída (schemas)
- **pandas 2** — agregação de stats
- **nba_api 1.11.4** — fonte de dados (endpoints não-oficiais da NBA.com)
- Cache **em memória** (snapshots ao vivo) + **em disco** (`PersistentCache`)
- Deploy: **Docker** (Railway). Branch de deploy: **`main`**.

> Não há banco de dados nesta branch. A área logada (Postgres + JWT) vive
> na branch `feature/login-area` e **não está em produção**.

---

## Arquitetura

Três camadas, dependências sempre apontando "pra dentro":

```
HTTP (main.py)  →  services/  →  utils/        + schemas/ (contrato)
   rotas            negócio       funções puras
                    + I/O externo  (sem I/O)
```

- **`utils/`** — funções determinísticas, sem rede e sem estado (fórmulas
  de linha, heat, projeção, blowout; agregação de PBP; cache; conversores).
- **`services/`** — regra de negócio + I/O externo (NBA API, nbarotations,
  The Odds API). Cada subpasta é um domínio isolado.
- **`schemas/`** — modelos Pydantic; o contrato público da API.
- **`workers/`** — tarefas de fundo (polling do scoreboard, pré-aquecimento
  de cache, backfill diário de predição × resultado).
- **`cache/`** — `InMemoryLiveGamesCache`, o snapshot ao vivo servido pelas
  rotas sem bater na NBA a cada request.

### Tempo real = **polling** (não há WebSocket)

O "ao vivo" do ClutchPro é **HTTP polling**, de propósito — **não usamos
WebSocket nem SSE em lugar nenhum**. O fluxo:

1. Um worker de fundo (`workers/live_games_worker.py`) consulta o scoreboard
   da NBA a cada `LIVE_POLL_INTERVAL_MS` (default **2s**) e grava o resultado
   no `InMemoryLiveGamesCache`.
2. `GET /games/live/today` devolve esse snapshot já pronto — instantâneo,
   **sem chamada à NBA por request**. A resposta inclui `updated_at` e
   `age_ms` pra o cliente saber a idade do dado.
3. O frontend faz `setInterval` nos endpoints REST (5–30s conforme a tela)
   e para de pollar quando o jogo está `final`.

**Por que não WebSocket:** a própria `nba_api` só atualiza a cada ~2–5s e
tem atraso de 30–60s vs o jogo real — um canal push não entregaria dados
mais frescos. Polling com cache no servidor é mais simples, resiliente a
falha da NBA (serve o último snapshot) e escala bem com o volume atual.

---

## Instalação

```bash
python -m venv .venv
source .venv/Scripts/activate     # Windows (Git Bash)
# source .venv/bin/activate       # Linux/macOS

pip install -r requirements.txt
```

Em caso de arquivos travados no `.venv`, apague e recrie:
```bash
rm -rf .venv && python -m venv .venv && source .venv/Scripts/activate
pip install -r requirements.txt
```

---

## Como rodar

```bash
uvicorn src.main:app --reload
```

- API: `http://localhost:8000`
- Docs interativos (Swagger): `http://localhost:8000/docs`

**Dev sem jogos ao vivo:** set `USE_FIXTURES=1` para ler de
`tests/fixtures/` em vez de bater na NBA Live API.

---

## Configuração (variáveis de ambiente)

Todas têm default sensato — a API sobe sem nenhuma configurada.

| Variável | Default | Para que serve |
|---|---|---|
| `PORT` | `8000` | Porta HTTP |
| `HOST` | `127.0.0.1` | Bind (cloud usa `0.0.0.0` via Docker CMD) |
| `ALLOWED_ORIGINS` | localhost + `*` | CORS (CSV de origens; `*` libera tudo) |
| `ENABLE_LIVE_WORKER` | `true` | Liga o worker de polling do scoreboard |
| `LIVE_POLL_INTERVAL_MS` | `2000` | Cadência do polling do scoreboard |
| `USE_FIXTURES` | `0` | Lê fixtures locais em vez da NBA Live API |
| `CACHE_DIR` | `/tmp` | Diretório do `PersistentCache` (use volume!) |
| `STATS_PROXY` | — | Proxy HTTP para `stats.nba.com` (IPs cloud são bloqueados) |
| `ENABLE_NBA_ROTATION_ADJUSTMENT` | `1` | Usa rotação real (nbarotations) na projeção |
| `NBA_ROTATION_BASE_URL` | `nbarotations.info` | Fonte dos perfis de rotação |
| `NBA_ROTATION_CACHE_TTL_SECONDS` | `604800` (7d) | TTL do cache de perfis de rotação |
| `ENABLE_REAL_ODDS` | `0` | Liga linhas reais (The Odds API) lado a lado com a synthetic |
| `ODDS_API_KEY` | — | Chave da The Odds API (necessária se `ENABLE_REAL_ODDS=1`) |
| `ODDS_REGIONS` | `us` | Região de mercado (`us`/`uk`/`eu`/`au`) |
| `ODDS_BOOKMAKERS` | — | CSV de books específicos (vazio = todos da região) |
| `LOG_LINE_CALC` | — | `1` liga o log JSONL de calibração + backfill diário |

> **Produção:** monte um volume persistente e aponte `CACHE_DIR` pra ele
> (ex.: `/data`). Sem volume, todo redeploy re-aquece o cache do zero —
> custa créditos de proxy e deixa o primeiro request lento.

---

## Endpoints

### Health & operação
| Rota | Descrição |
|---|---|
| `GET /health` | Liveness check |
| `GET /live/cache/status` | Estado do snapshot ao vivo (idade, nº de jogos) |

### Jogadores — histórico
| Rota | Descrição |
|---|---|
| `GET /players/search?name=` | Busca por nome → `id`, `full_name`, `is_active` |
| `GET /players/{id}/gamelog?season=` | Game log da temporada |
| `GET /players/{id}/stats/games?season=` | Stats jogo a jogo (para gráficos) |
| `GET /players/{id}/analysis/season?season=` | Médias gerais + L5/L10 + trend |
| `GET /players/{id}/analysis/points-by-period?season=&last_games=` | Média de pontos por quarto¹ |
| `GET /players/{id}/dashboard?season=&last_games=` | Combina temporada + recents + períodos + trend |

### Liga (cacheado, barato — 2–3 chamadas / 30 min)
| Rota | Descrição |
|---|---|
| `GET /players/hot-board?season=` | "Jogadores quentes da liga" — forma L5/L10 vs média |
| `GET /teams/board?season=` | Pontos marcados/sofridos por time |

### Jogos — ao vivo & contexto
| Rota | Descrição |
|---|---|
| `GET /games/live/today` | Jogos do dia (snapshot do cache, instantâneo) |
| `GET /games/{id}/live-boxscore` | Boxscore em tempo real |
| `GET /games/{id}/live-analysis?season=` | Análise de todos os jogadores em quadra |
| `GET /games/{id}/live-hot-ranking?season=&limit=&consider_blowout=` | Ranking dos mais quentes + projeção + linha |
| `GET /games/{id}/lineups` | Titulares / reservas / inativos + split por quarter |
| `GET /games/{id}/rotations` | Heatmap histórico de minutos (nbarotations) |
| `GET /games/{id}/preview?season=` | Briefing pré-jogo (médias + recents + linhas) |
| `GET /games/{id}/probable-lineup?season=` | Escalação provável + forma L3/L5/L10 (informativo) |
| `GET /games/{id}/play-by-play` | PBP de um jogo |
| `GET /players/{pid}/games/{gid}/points-by-period` | Pontos por quarto (jogo único) |
| `GET /players/{pid}/games/{gid}/live-comparison?season=` | Comparação individual ao vivo vs média |

### Admin & debug
| Rota | Descrição |
|---|---|
| `GET /admin/calibration/report?window_days=` | Relatório de erro da linha sintética (precisa `LOG_LINE_CALC=1`) |
| `GET /admin/calibration/dataset-status` | Quanto dado já há pra treinar o calibrador |
| `GET /admin/backtest?min_decision=&odds_american=` | Backtest hipotético sobre o log JSONL |
| `GET /debug/server-ip` | IP público do servidor (para whitelist de proxy) |
| `GET /debug/proxy-test` | Testa o proxy em HTTP e HTTPS |
| `GET /debug/nba-stats` | Diagnóstico: bate em `stats.nba.com` direto |

¹ Faz 1 chamada PBP por jogo — pode levar 30–120s com `last_games=10`.

**`season`** sempre no formato `YYYY-YY` (ex.: `2024-25`). **`game_id`** sai
de `/games/live/today` (ao vivo) ou do `gamelog` (histórico).

### Fórmula do `live-hot-ranking` / `live-analysis`

Para cada jogador que já entrou em quadra:
```
expected_X        = season_avg_X × (current_minutes / season_avg_minutes)
shooting_impact   = bônus por acertos/volume acima do esperado
                  − penalidade por erros acima do esperado
score             = (pts_diff × 0.85) + (reb_diff × 0.6)
                  + (ast_diff × 0.7)  + shooting_impact
```

| Score | Status |
|---|---|
| ≥ 5 | hot |
| ≥ 2 | above_average |
| > −2 | normal |
| > −5 | below_average |
| ≤ −5 | cold |

> A 1ª chamada busca a média da temporada de cada jogador (1 chamada/jogador
> à NBA). Com ~20 jogadores, 1–3 min. As seguintes (dentro do TTL) são
> instantâneas. O `season_cache_warmer` pré-aquece isso no startup.

---

## Estrutura do projeto

```
src/
├── main.py                       FastAPI app + todas as rotas + lifespan
├── config.py                     env vars + feature flags
├── nba_api_patches.py            injeta headers Referer/Origin (NBA exige desde mai/2026)
│
├── schemas/                      Pydantic — contrato da API
│   ├── nba_schemas.py            player, gamelog, PBP, hot-board, team-board, probable-lineup
│   ├── analysis_schemas.py       season analysis, dashboard, stats por jogo, períodos
│   ├── live_schemas.py           today games, boxscore, hot ranking, lineup, rotations, preview
│   └── anomaly_schemas.py
│
├── utils/                        funções puras, sem I/O
│   ├── stats.py                  linha sintética, edge, heat, projeção, blowout, faltas
│   ├── pbp_aggregator.py         agrega PBP por player × período
│   ├── cache.py                  SimpleCache (mem) + PersistentCache (disco)
│   ├── converters.py             team_name→tricode, parsers de evento
│   ├── time_utils.py             relógio de jogo, parsing de período/clock
│   └── photos.py                 URLs de headshot/logo
│
├── cache/
│   └── live_games_cache.py       InMemoryLiveGamesCache (snapshot servido pelas rotas)
│
├── services/                     negócio + I/O externo
│   ├── nba_service.py            stats.nba.com (gamelog, search, PBP)
│   ├── player_analysis_service.py season analysis, dashboard, períodos
│   ├── live_game_service.py      scoreboard, boxscore, lineup, rotations
│   ├── live_pbp_service.py       PBP live + agregação por período
│   ├── live_analysis_service.py  orquestra hot ranking / preview / comparison
│   ├── anomaly_service.py        detector de outliers ao vivo
│   ├── hot_board_service.py      "jogadores quentes da liga" + probable-lineup
│   ├── team_board_service.py     pontos marcados/sofridos por time
│   ├── cashout.py                heurística de cashout
│   │
│   ├── line/                     linha sintética + logging + calibração
│   │   ├── line_engine.py        fachada do cálculo de linha
│   │   ├── line_log.py           JSONL pra dataset de calibração
│   │   ├── line_calibration.py   relatório agregado de erros
│   │   └── line_calibrator.py    recalibração ML-based (em desenvolvimento)
│   │
│   ├── projection/
│   │   └── projection_engine.py  projeção fim-de-jogo por stat
│   ├── matchup/
│   │   └── matchup_provider.py   DRtg + pace por time
│   ├── hot_streak/
│   │   └── heat_detector.py      score composto −1..+1
│   ├── similar_games/
│   │   └── analyzer.py           "jogos parecidos" no histórico
│   │
│   ├── rotation/                 perfil de rotação (nbarotations.info)
│   │   ├── rotation_provider.py  fetch + cache do perfil
│   │   ├── rotation_derivation.py janelas, clutch, blowout
│   │   ├── rotation_context.py   classificação + notas humanas (minutos vs esperado)
│   │   ├── production_by_period.py rates pts/ast/reb por período
│   │   ├── nbarotations_client.py
│   │   └── nbarotations_parser.py
│   │
│   ├── odds/                     linhas reais (The Odds API) — opcional
│   │   ├── odds_service.py       cache TTL dinâmico por estado do jogo
│   │   ├── odds_client.py
│   │   ├── player_matcher.py     match nome → player_id NBA
│   │   └── team_mapping.py
│   │
│   └── backtester/               hit rate / ROI sobre o JSONL de log
│       ├── backtester.py
│       ├── historical.py · historical_loader.py · snapshot.py
│
└── workers/                      tarefas de fundo (asyncio, no mesmo processo)
    ├── live_games_worker.py      polling do scoreboard → cache
    ├── season_cache_warmer.py    pré-aquece médias da temporada
    └── backfill_worker.py        backfill diário predição × resultado
```

Docs complementares em `docs/`:
- `docs/sistema-projecao.md` — como a projeção é calculada.
- `docs/lineups.md` — montagem do elenco / split por quarter.
- `docs/logging-producao.md` — split stdout/stderr e logging no Railway.

---

## Cache & custo

| Dado | TTL | Onde |
|---|---|---|
| Scoreboard (jogos do dia) | polling 2s → snapshot | memória |
| Boxscore live | 15s | memória |
| Médias da temporada (jogador) | ~24h | disco |
| Hot board / team board (liga) | 30 min | disco |
| Perfil de rotação | 7 dias | disco |
| Odds | TTL dinâmico (30–120s por estado do jogo) | memória |

Princípio de custo: preferir **chamadas agregadas da liga** (2–3 por 30 min,
compartilhadas entre hot-board, probable-lineup e team-board) a chamadas por
jogador/jogo. IPs de cloud são bloqueados por `stats.nba.com` — em produção
roteie via `STATS_PROXY` (proxy residencial).

---

## Integrações externas

- **nba_api** (`stats.nba.com` + `cdn.nba.com` live) — fonte primária.
  `nba_api_patches.py` injeta `Referer`/`Origin` (a NBA passou a exigir em
  mai/2026; sem isso → 403).
- **nbarotations.info** — padrões históricos de rotação (heatmap de 48 min).
  Liga/desliga via `ENABLE_NBA_ROTATION_ADJUSTMENT`.
- **The Odds API** — linhas reais de player props (PTS/REB/AST), **opcional**.
  Off por padrão; liga com `ENABLE_REAL_ODDS=1` + `ODDS_API_KEY`. Sem ela, o
  schema devolve `real_line=None` e o front mostra só a linha sintética.

---

## Testes

```bash
pytest                 # suíte completa
pytest -q tests/test_live_analysis_service.py   # arquivo único
```

28 arquivos de teste cobrindo services, fórmulas (utils), workers e o
merge de scoreboard. Fixtures de jogos reais em `tests/fixtures/`.

---

## Deploy

`Dockerfile` (Python 3.12-slim) instala `requirements.txt`, copia `src/` e
sobe `uvicorn src.main:app --host 0.0.0.0 --port ${PORT}`. No Railway:

1. Configure as env vars (no mínimo `STATS_PROXY` se a NBA bloquear o IP).
2. Monte um **volume persistente** e set `CACHE_DIR` pra ele.
3. CORS já aceita `*.railway.app`, `*.vercel.app` e `*.nine6.com.br` via
   regex — não precisa atualizar `ALLOWED_ORIGINS` para esses domínios.

---

## Limitações da `nba_api`

| Limitação | Detalhe |
|---|---|
| Delay | Dados live atrasam ~30–60s vs o jogo real |
| Rate limiting | Muitas chamadas seguidas são bloqueadas pela NBA.com |
| Instabilidade | Endpoints live podem falhar durante jogos de alta carga |
| Inconsistência | `minutes` e outros campos mudam de formato ao longo da temporada |
| Sem suporte oficial | Usa endpoints não documentados — podem mudar sem aviso |

**Migração futura (se virar produção séria):** SportsDataIO (delay < 5s),
Sportradar (premium, odds integrados) ou Stats Perform (enterprise).

---

## Próximos passos

1. **Cache compartilhado** — Redis para compartilhar cache entre instâncias.
2. **Persistência** — Postgres para game logs e validação de Hot Picks
   (predição × resultado). Base já existe na branch `feature/login-area`.
3. **Jobs de ingestão** — atualizar histórico automaticamente após cada rodada.
4. **Calibração ML** — finalizar `line_calibrator.py` com o dataset do log.
5. **Auth em produção** — promover a área logada (`feature/login-area`).
</content>
</invoke>
