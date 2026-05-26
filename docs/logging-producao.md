# Logging de produção (predição × resultado real)

Guia prático pra **ligar** a coleta de dados reais no Railway. O código
já está pronto e testado — isso aqui é só a parte operacional (config no
painel, ~15 min). Sem isso, toda calibração do sistema continua sendo
hipótese; com isso, vira fato medido.

---

## O que isso faz (resumo)

Dois passos automáticos:

1. **Logging** (em tempo real, dentro do jogo): toda vez que o sistema
   calcula uma linha/projeção, grava 1 registro em
   `CACHE_DIR/line_log.jsonl` — *"prevejo LeBron PTS 32.5, linha 30.5"*.
   O resultado fica vazio (jogo não acabou).

2. **Backfill** (1×/dia, `scripts/backfill_outcomes.py`): volta nos
   registros sem resultado, busca quanto o jogador fez de verdade no
   jogo já encerrado, e preenche. No mesmo passo faz a manutenção
   (retenção 90d + teto 200MB).

Depois de ~30 dias você tem dataset real pra medir CLV/acerto contra a
linha do book — não contra linha sintética.

Os **3 freios** garantem que o arquivo nunca cresce sem limite:
dedup (1 registro por jogador/stat/minuto), retenção (90 dias), teto
(200 MB). Custo previsível: ~$0.40/mês de volume.

---

## Passo 1 — Volume persistente (CRÍTICO, não pular)

O filesystem do Railway é **efêmero**: todo deploy zera o disco. Sem um
volume persistente, o `line_log.jsonl` some a cada deploy e você nunca
acumula histórico. **Esse é o único ponto que não pode faltar.**

No painel do Railway, no serviço do **backend (API)**:

1. Aba **Settings** → seção **Volumes** → **+ New Volume**
2. **Mount path**: use o mesmo caminho que o `CACHE_DIR` aponta.
   - Confira o valor atual em **Variables** (env `CACHE_DIR`).
   - Se não existir `CACHE_DIR`, o default do código é relativo ao
     projeto — **defina explicitamente** `CACHE_DIR=/data` (passo 2) e
     monte o volume em **`/data`**.
3. Tamanho: **1 GB** é folgado (uma temporada deduped ≈ 1.5 GB no
   pior caso; o teto de 200MB do prune mantém bem abaixo). Pode começar
   com 1 GB.
4. Salvar. O Railway vai redeployar com o volume montado.

> Verificação: depois do deploy, o caminho do volume deve existir e ser
> gravável. O código cria o arquivo sozinho na primeira escrita.

---

## Passo 2 — Variáveis de ambiente

No serviço do backend, aba **Variables**, adicione/confirme:

| Variável | Valor | Pra quê |
|---|---|---|
| `LOG_LINE_CALC` | `1` | Liga o logging (default é `0` = desligado) |
| `CACHE_DIR` | `/data` | Onde grava o log — **tem que ser o mount path do volume** do passo 1 |

Salvar → redeploy automático. A partir daí, todo jogo ao vivo começa a
gravar predições.

---

## Passo 3 — Cron? NÃO precisa. Roda sozinho no app.

O Railway tirou o "Cron Service" do menu e, de qualquer forma, **não é
necessário**. O backend já fica Online 24/7 → o backfill roda **dentro
do próprio processo**, 1×/dia, automaticamente.

`src/workers/backfill_worker.py` agenda o run diário às **12:00 UTC**
(~09:00 BRT — a rodada da NBA da noite anterior já encerrou com folga).
Faz o backfill **e** o prune (retenção 90d + teto 200MB) no mesmo passo.

**Vantagens de ser in-app:**
- Zero serviço/cron extra no Railway
- Roda no mesmo processo que tem o volume → **zero config de volume
  compartilhado** (o problema que existiria com Cron Service separado)
- Custo adicional: zero

**Gate automático:** o worker só inicia se `LOG_LINE_CALC=1` (passo 2).
Sem logging não há o que preencher. Se desligar o logging, o worker
não sobe — sem ação manual.

**Best-effort:** se um run falhar (NBA API fora, etc.), loga e tenta de
novo no dia seguinte. Nunca derruba o app.

### Rodar manualmente (opcional — debug / forçar agora)

Não é necessário pro funcionamento, mas se quiser rodar na mão:

```bash
python scripts/backfill_outcomes.py --dry-run         # só reporta, não escreve
python scripts/backfill_outcomes.py                   # backfill + prune
python scripts/backfill_outcomes.py --no-prune        # backfill sem manutenção
python scripts/backfill_outcomes.py --max-age-days 60 # retenção customizada
python scripts/backfill_outcomes.py --max-size-mb 100 # teto customizado
```

Saída esperada (exemplo):
```
Backfill em: /data/line_log.jsonl
{ "scanned": 4210, "filled": 3980, "skipped_no_game_id": 0, ... }
Prune (retenção + teto):
{ "total": 4210, "kept": 4210, "dropped_age": 0, "dropped_size": 0,
  "final_size_mb": 3.1 }
```

Nos logs do Railway você vê 1×/dia:
`Backfill diário concluído: {...}` + `Prune diário (retenção+teto): {...}`

---

## Passo 4 — Ler o resultado (depois de ~30 dias)

Com dataset real acumulado, roda o backtester clássico (lê o
`line_log.jsonl` real, não linha sintética):

```bash
python -m src.services.backtester      # backtester que lê o log real
```

Aí sim você vê hit rate / CLV contra a linha do **book real**, por
mercado, por decisão. É o número que vale — diferente do backtester
de linha sintética que usamos pra calibrar.

---

## Checklist rápido

- [ ] Volume persistente montado no `CACHE_DIR` (você já tem ✅)
- [ ] `CACHE_DIR` = mount path do volume (já, senão o cache nem funcionaria)
- [ ] **`LOG_LINE_CALC=1` no env do backend** ← único passo que falta
- [ ] Redeploy (Railway faz sozinho ao salvar a variável)
- [ ] (Depois de 1-2 dias) conferir nos logs: `Backfill diário concluído`
      aparece 1×/dia, e o arquivo cresce dia a dia

> **Não precisa**: criar Cron Service, volume separado, ou configurar
> agendamento. O worker in-app cuida de tudo quando `LOG_LINE_CALC=1`.

---

## Troubleshooting

| Sintoma | Causa provável | Fix |
|---|---|---|
| Arquivo sempre vazio | `LOG_LINE_CALC` não é `1`, ou não tem jogo ao vivo | Conferir env + esperar jogo |
| Arquivo some no deploy | Volume não montado / `CACHE_DIR` errado | Passo 1 + 2 |
| `actual_outcome` sempre `null` | Worker in-app não subiu (LOG_LINE_CALC≠1) ou ainda não bateu 12:00 UTC | Conferir env + logs `Backfill diário` |
| Arquivo crescendo demais | Dedup só funciona com `game_id`; conferir que os registros têm `game_id` | Já é o padrão; checar com `--dry-run` |
| Backfill não acha o jogo | PBP histórico ainda não disponível (rodar muito cedo) | Agendar cron mais tarde |

---

## Por que isso importa (lembrete)

Tudo que calibramos olhando o backtester de **linha sintética** é
hipótese. Esse pipeline dá o backtester de **dados reais**. É a
diferença entre *"achamos que melhorou"* e *"sabemos que melhorou"*.
Custo: ~$0.40/mês + 15 min de setup. Maior alavancagem por menor
esforço do projeto inteiro.
