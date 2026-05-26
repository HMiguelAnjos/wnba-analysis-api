# Lineups — origem dos dados e como ajustar

Endpoint: `GET /games/{game_id}/lineups` → `LineupGameSchema`

## Tudo que vem oficialmente da NBA Live API

Todos os campos abaixo vêm direto do JSON do boxscore live
(`cdn.nba.com/static/json/liveData/boxscore/boxscore_<gameId>.json`)
sem inferência. Confiança = "alta".

| Campo do schema | Fonte | Tipo na NBA |
|---|---|---|
| `is_starter` | `player.starter` | "1"/"0" |
| `is_on_court` | `player.oncourt` | "1"/"0" |
| `played` | `player.played` | "1"/"0" |
| `position` | `player.position` | "PG"/"SG"/"SF"/"PF"/"C"/"" |
| `status` | `player.status` | "ACTIVE"/"INACTIVE" |
| `not_playing_reason` | `player.notPlayingReason` | string ou null |
| `jersey_num` | `player.jerseyNum` | string |
| stats | `player.statistics.*` | int |

## Coisas computadas (não vêm da NBA)

### `photo_url`
URL determinística construída a partir do `personId`:
```
https://cdn.nba.com/headshots/nba/latest/260x190/{personId}.png
```
Não validamos prévio — o CDN serve uma silhueta padrão pra IDs
desconhecidos (sempre 200). Front lida com erro de rede via `<img onError>`.

### `performance_rating` (0–10) e `performance_label`
Função pura `calculate_player_performance_rating` em `src/utils/stats.py`.

Combina:
- Pontos × 0.50
- Rebotes × 0.70
- Assistências × 0.90
- Roubos × 1.80
- Tocos × 1.80
- Turnovers × −1.20
- Faltas × −0.30
- +/− × 0.10
- Bônus de eficiência (eFG% acima/abaixo de 50%)
- Bônus de free-throw (acima/abaixo de 75%)

Normalização: `5.0 + raw × 0.18`, clamped em [0, 10].

Penalidade: jogador com <10 minutos é flagged `low_confidence` e
nota fica capped em 7.0 (não dá pra dar 9.0 pra alguém que jogou 4 min).

Labels:
- `>= 8.5` → Excelente
- `>= 7.0` → Bom
- `>= 5.0` → Regular
- `> 0.0` → Ruim
- `== 0` → N/A (jogador não entrou em quadra)

## Limitações conhecidas

1. **Nota é heurística simples.** Não considera contexto (quem é o
   adversário, garbage time, importância das jogadas). Para análise
   profunda, use a aba Ao Vivo que tem comparação contra média da
   temporada.

2. **`is_on_court` pode ficar "0" momentaneamente entre jogadas.**
   A NBA atualiza esse campo nas substituições. No card detalhado
   isso aparece como `🪑 No banco` mesmo se ele acabou de sair pra
   reentrar. Não é um bug do nosso código — é o estado real do
   boxscore daquele instante.

3. **`starter` só fica disponível depois do jump ball.** Em jogos
   ainda não iniciados, todos os jogadores aparecem como bench.
   Por isso o endpoint só faz sentido com `game_status != not_started`
   (front bloqueia automaticamente).

4. **Foto pode estar desatualizada para jogadores recém-trocados.**
   O CDN da NBA atualiza a foto em latência variável após a troca.
   Não há solução nossa — é só esperar.

## Como ajustar a fórmula da nota

Edite os pesos em `src/utils/stats.py`:

```python
_RATING_WEIGHTS = {
    "points":    0.50,   # ← aumentar pra valorizar mais pontos
    "rebounds":  0.70,
    "assists":   0.90,
    "steals":    1.80,
    "blocks":    1.80,
    "turnovers": -1.20,
    "fouls":     -0.30,
    "plus_minus": 0.10,
}
```

Os testes em `tests/test_performance_rating.py` cobrem casos típicos
(jogador médio, estrela, ruim, low confidence). Rodar com:

```bash
pytest tests/ -v
```

A normalização final (`5.0 + raw × 0.18`) também é ajustável para
calibrar a escala — diminuir o multiplier comprime as notas perto
do meio; aumentar espalha mais pros extremos.
