# Fixtures locais

Esta pasta guarda JSONs **reais** baixados da NBA Live API, usados pra
testar o backend quando não tem jogo ao vivo (madrugada, off-season, etc).

**Os arquivos `.json` são ignorados pelo git** — cada dev baixa os seus.

## Como gerar

Da raiz do repo:

```bash
python scripts/fetch_fixtures.py
```

Isso salva 3 arquivos:

- `scoreboard_today.json` — lista de jogos atual
- `boxscore_blowout_final.json` — Celtics x Mavs G5 (blowout decidido, 39 pts)
- `boxscore_moderate_blowout.json` — Finals G1 (blowout moderado, 18 pts)

## Como rodar o backend usando as fixtures

```bash
USE_FIXTURES=1 uvicorn src.main:app --reload
```

Em modo `USE_FIXTURES=1`, o `live_game_service`:

- Lê o scoreboard de `scoreboard_today.json`
- Lê qualquer boxscore de `boxscore_<gameId>.json` se existir,
  caindo em `boxscore_blowout_final.json` como fallback

## Cenários úteis pra testar

**Jogo decidido em blowout** → `boxscore_blowout_final.json`
- Margem 39 pts → `BlowoutRiskSchema.percentage` ≈ 90%, level "final"
- Titulares ganham flag `💥 Risco de descanso` past-tense
- Projeções viram "estatísticas finais" (sem range, sem extrapolação)

**Blowout moderado** → `boxscore_moderate_blowout.json`
- Margem 18 pts → meio-termo
- Útil pra testar a faixa medium

## Por que não commitar

1. **Tamanho:** cada boxscore tem ~35KB. Vão acumulando se cada PR baixa novos.
2. **Volatilidade:** o scoreboard de hoje fica obsoleto amanhã.
3. **Direitos:** dados da NBA — melhor cada dev ter os seus em vez de redistribuir no repo.
