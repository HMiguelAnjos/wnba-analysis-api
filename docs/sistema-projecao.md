# 📘 ClutchPro — Como o Sistema Decide um Pick

> Guia de leitura fácil — sem jargão. Pra qualquer pessoa entender por que um
> jogador aparece em Hot Picks (ou não) e como o número de projeção é calculado.

---

## A pergunta que o sistema responde

**"Quantos pontos (ou rebotes, ou assistências) esse jogador vai terminar o jogo?"**

A partir dessa resposta, o sistema compara com a linha do book (Bet365, etc.) e diz:
- 🟢 OVER tem valor (projeção MAIOR que a linha)
- 🔴 UNDER tem valor (projeção MENOR que a linha)
- ⚪ Sem valor (projeção próxima da linha)

---

## Como o sistema chega no número

### A conta básica

```
projeção final = pontos atuais + (ritmo) × (minutos que ele ainda vai jogar)
```

Exemplo: cara fez **10 pontos em 15 minutos**, está num **ritmo de 0.66 pts/min**.
Se ele jogar mais **12 minutos**:

```
projeção = 10 + (0.66 × 12) = 10 + 8 = 18 pontos
```

Simples assim. **O segredo está em calcular bem o "ritmo".**

---

### Como o "ritmo" é calculado

O ritmo não é só o que ele tá fazendo agora — é uma **mistura entre 3 coisas**:

#### 1️⃣ O que ele faz normalmente (histórico)

Pegamos a **média histórica dele**, calculada com peso:
- **55%** da temporada toda
- **30%** dos últimos 10 jogos
- **15%** dos últimos 5 jogos

Chamamos isso de **"média histórica"** (no debug aparece como `prior_avg`).

Exemplo: cara que fez 20 pts/jogo na temporada, 22 nos últimos 10, e 24 nos últimos 5:
```
média histórica = 0.55×20 + 0.30×22 + 0.15×24 = 11 + 6.6 + 3.6 = 21.2 pontos
```

#### 2️⃣ O ritmo do jogo atual

Quantos pontos ele está fazendo **por minuto agora**.
```
ritmo atual = pontos no jogo / minutos jogados
```

#### 3️⃣ O padrão dele neste quarter específico

Alguns jogadores rendem mais no Q4 (LeBron faz mais pts no fim), outros no Q1.
O sistema usa o histórico do jogador **neste mesmo quarter** dos últimos jogos.

---

### A "balança" entre histórico e ao vivo

O sistema **mistura** essas 3 fontes pesando cada uma. Quanto mais minutos o cara já jogou
+ quanto mais "fora do normal" ele está (positivo ou negativo), mais o sistema confia no ao vivo
e menos no histórico.

| Situação | Quanto o ao vivo pesa |
|---|---|
| Cara dentro do esperado | até **85%** |
| Cara claramente quente (boas estatísticas + mais de 15 min jogados) | até **90%** |
| Cara em career night (muito quente + mais de 20 min jogados) | até **95%** |

**Filosofia**: quanto mais sinais convergentes, mais o ao vivo manda.

---

## Proteções contra distorções

O sistema tem **freios** pra não projetar números absurdos em situações de pouca informação.

### 🛡 Cara com pouco tempo de jogo (anti-ruído)

| Quando | O que faz |
|---|---|
| Cara fez **0 pontos em menos de 8 minutos** + ele normalmente faz 3+ ppg | Sistema NÃO projeta nada (mostra "—"). Sample muito pequena pra confiar. |
| Cara fez **muito mais** que o normal em **menos de 8 min** (ex: 8 pts em 5 min) | Sistema "puxa pra baixo" — não vai projetar 35 pts pra ele baseado em 5 min |

### 🛡 Cara frio (anti-projeção-absurda-pra-baixo)

Mesmo cara que tá **claramente mal hoje** não termina o jogo em zero produção:
- **Piso de 70%**: a projeção do que ele ainda vai produzir **nunca** desce abaixo de 70% do ritmo histórico dele
- Isso evita projetar 2 pts pra um cara de 15 ppg só porque ele fez 0 em 15 minutos

### 🛡 Cara quente (anti-projeção-absurda-pra-cima)

Mesmo cara em career night tem um **teto**:
- Cara normal: projeção máxima é **1.8× a média histórica** (cara de 20 ppg → teto em 36)
- Cara claramente quente: teto sobe pra **2.6×** (cara de 20 ppg → 52)
- Tudo isso evita projeções absurdas tipo "70 pts pro Lebron porque tá com 20 em 10 min"

### 🛡 Minutos que ele ainda vai jogar

O sistema pega 3 fontes pra estimar quantos minutos ele ainda vai jogar:
1. Histórico de rotação dele (nbarotations.info)
2. Padrão geral (minutos médios × fração de jogo restante)
3. **Floor**: se ele já jogou mais que o normal e está quente, garantimos que ele jogue mais
   - **30% do jogo restante** se ele superou a minutagem média
   - **40%** se também está com termômetro quente
   - **50%** se está em career night

E nunca pode passar do **tempo físico do jogo** (cara não pode jogar mais minutos do que sobra no relógio).

---

## O "termômetro de calor" (heat)

Pra saber se o cara está **quente** ou **frio**, combinamos vários sinais:

- **Eficiência de arremesso** vs a média dele (acertando bem?)
- **Volume**: ele está chutando mais que o normal?
- **Agressividade**: indo pro lance livre mais que o normal?
- **Sequência**: 3+ jogadas consecutivas pontuando?

Cada sinal vira um número entre -1 e +1. Combinados, dão o "heat" final:

| Heat | Significado |
|---|---|
| **+0.6 a +1.0** | 🔥 Muito quente |
| **+0.3 a +0.6** | 🟢 Quente |
| **-0.3 a +0.3** | ⚪ Neutro |
| **-0.6 a -0.3** | 🟡 Frio |
| **-1.0 a -0.6** | 🥶 Muito frio |

Esse termômetro **modifica a projeção**:
- Quente: acelera a projeção (até +30% no que falta)
- Frio: reduz a projeção (até -30% no que falta)
- Neutro: não mexe

---

## A decisão (OVER, UNDER, NEUTRO)

Depois de calcular a projeção, comparamos com a linha do book:

```
edge = projeção − linha
```

Se edge **positivo**: projetamos MAIS que a linha → **OVER**
Se edge **negativo**: projetamos MENOS que a linha → **UNDER**

### Os 5 tipos de decisão

| Edge | Decisão | Cor | O que significa |
|---|---|---|---|
| **≥ +3.0** | STRONG_OVER | 🟢 Verde forte | Aposta OVER com convicção |
| **+1.5 a +3.0** | LEAN_OVER | 🟢 Verde | Tendência OVER |
| **-2.0 a +1.5** | NEUTRAL | ⚪ Cinza | Sem aposta clara |
| **-4.0 a -2.0** | LEAN_UNDER | 🔴 Vermelho | Tendência UNDER |
| **≤ -4.0** | STRONG_UNDER | 🔴 Vermelho forte | Aposta UNDER com convicção |

### Por que OVER e UNDER têm limites diferentes?

OVER precisa de menos edge (+1.5) que UNDER (-2.0). Motivo:

- **OVER**: cara ainda pode produzir. O "teto" dele tá aberto — pode estourar.
- **UNDER**: precisa o cara PARAR de produzir. Variação geralmente joga **contra** você.

Imagina LeBron com 17 pts em 27 min, linha 23.5:
- Pra UNDER bater, ele precisa fazer **menos de 7 pts** em ~10 min restantes
- Mas se ele só acertar 2 cestas e um 3, já bate 7+
- **Variação ajuda quem aposta OVER, prejudica quem aposta UNDER**

Por isso UNDER precisa de mais margem de segurança no edge.

---

## A recomendação de aposta (PASS, SMALL, MEDIUM, LARGE)

Mesmo o sistema falando "OVER", **ele ainda decide o TAMANHO da aposta** baseado em:

1. **Edge** (tamanho da vantagem)
2. **Confiança** (qualidade do sinal — sample, alinhamento entre indicadores)
3. **Importância do jogador** no time

### A matriz de tamanho

| Tamanho | Edge mínimo | Confiança mínima |
|---|---|---|
| **LARGE** | ≥ 3.0 | ≥ 75% (≥ 70% se for star primário) |
| **MEDIUM** | ≥ 2.0 | ≥ 60% (≥ 55% se for star primário) |
| **SMALL** | ≥ 1.0 | ≥ 45% |
| **PASS** | qualquer outro | qualquer outro = NÃO APOSTAR |

**Hot Picks só mostra picks com SMALL, MEDIUM ou LARGE. PASS é filtrado.**

### Penalidades especiais

- **Cara reserva (joga pouco, usage < 30%)**: nunca chega em MEDIUM ou LARGE. Cap em SMALL.
- **Star primário (usage > 65%)**: ganha bônus — alcança LARGE com confiança 70% (em vez de 75%).
- **Confiança muito baixa (< 40%)**: vira PASS automático, mesmo com edge alto.

### O "Book Bypass" (mai/2026)

Exceção pra confiança baixa: quando **o book real (Bet365) concorda** com nossa direção
e o edge real é ≥ 2.0, aceitamos SMALL mesmo com confiança baixa.

**Por quê?** Se DOIS sistemas independentes (nosso modelo + o livro de aposta) chegam na
mesma conclusão, falso positivo é menos provável. Mas:
- Confiança ainda precisa ser **≥ 30%** (filtra casos onde sinais brigam muito)
- Bypass nunca escala pra MEDIUM ou LARGE — só vira SMALL

---

## Cenários reais (10 exemplos)

Pra cada cenário, mostro: **quem o cara é** → **o que o sistema calcula** → **decisão final**.

### Cenário A — Star em career night 🔥

**Quem é**: ala-armador titular. Normalmente faz **22 pts em 35 min**.
**Como tá hoje**: 26 pts em 22 min, jogando demais bem (heat +0.85). Q3 com 6 min.
**Linha do book**: 31.5

**O sistema pensa**:
- Ritmo normal dele: 22 / 35 = **0.63 pts/min**
- Ritmo agora: 26 / 22 = **1.18 pts/min** (quase o dobro!)
- Heat altíssimo + mais de 20 min jogados → confiamos **95% no ao vivo**
- Career night confirmada → relaxa o teto, permite projeções altas

**Conta**:
- O cara provavelmente joga pelo menos 50% do tempo restante (~9 min)
- Vai produzir a ~1.15 pts/min nesses 9 min = +10.4 pts
- Com bônus de "muito quente" (+30%) no que falta = **+13.4 pts**
- **Projeção final: 39.5**
- **Edge: 39.5 − 31.5 = +8.0**

✅ **Decisão: STRONG_OVER (LARGE)** — vai pra Hot Picks como pick forte

---

### Cenário B — Star em jogo ruim 🥶

**Quem é**: pivô titular. Normalmente faz **16 pts em 30 min**.
**Como tá hoje**: só 4 pts em 18 min, errando muito (heat -0.55). Q3 com 8 min.
**Linha do book**: 14.5

**O sistema pensa**:
- Ritmo normal: 16 / 30 = **0.53 pts/min**
- Ritmo agora: 4 / 18 = **0.22 pts/min** (cara fazendo menos da metade do normal!)
- Frio confirmado por vários sinais → confia bastante no ao vivo (peso 65%)
- **Mas** aplica o piso de 70% → não desce projeção pra menos que 0.37 pts/min
- E NÃO aplica o "cut" do termômetro frio (anti-double-counting)

**Conta**:
- Resta 12 min de jogo
- Produção esperada nesses 12 min = 0.37 × 12 = +4.4 pts
- **Projeção final: 8.5**
- **Edge: 8.5 − 14.5 = -6.0**

✅ **Decisão: STRONG_UNDER (MEDIUM)** — vai pra Hot Picks

---

### Cenário C — Star borderline (caso real LeBron) ⚖

**Quem é**: forward titular. Normalmente faz **24 pts em 35 min**.
**Como tá hoje**: 17 pts em 27 min, neutro (heat +0.10). Q3 com 4 min.
**Linha do book**: 23.5

**O sistema pensa**:
- Ritmo normal: 24 / 35 = **0.69 pts/min**
- Ritmo agora: 17 / 27 = **0.63 pts/min**
- Apenas 9% abaixo do normal → **não é frio**, é só "dia OK"
- Mistura padrão (peso ~81% no ao vivo) → ritmo combinado ~0.64 pts/min
- Histórico do LeBron no Q3: ele relaxa (ritmo histórico ~0.55) → puxa pra baixo

**Conta**:
- Ritmo final misturado: 0.60 pts/min
- Restam ~8 min
- Produção esperada: +4.8 pts
- **Projeção final: 21.8**
- **Edge: 21.8 − 23.5 = -1.7**

⚪ **Decisão: NEUTRO** (edge entre -2 e +1.5)
❌ **NÃO aparece em Hot Picks** — filtrado por threshold

> Antes dos novos limites, esse mesmo caso virava LEAN_UNDER. Hoje é corretamente
> identificado como "sem aposta clara".

---

### Cenário D — Reserva explosivo 🚀

**Quem é**: reserva. Normalmente faz **8 pts em 11 min** (joga pouco).
**Como tá hoje**: 14 pts em 14 min — já superou minutagem normal, heat +0.65. Q3 com 10 min.
**Linha do book**: 14.5

**O sistema pensa**:
- Ritmo normal: 8 / 11 = **0.73 pts/min**
- Ritmo agora: 14 / 14 = **1.00 pts/min** (37% acima)
- Cara já passou da minutagem normal + tá quente → coach vai mantê-lo
- Floor de minutos: **40% do tempo restante** = 8.8 min adicionais

**Conta**:
- Mistura de ritmos (peso ~70% no ao vivo) = ~0.92 pts/min
- Bônus de heat moderado (+12%) → +12% no que falta
- 8.8 min × 0.92 = +8.1 pts × 1.12 = +9.1 pts
- **Projeção raw: 23.1**
- Mas **teto** entra: 8 × 2.1 = 16.8 (cap pra cara claramente fora do normal)
- **Projeção final: 16.8**
- **Edge: 16.8 − 14.5 = +2.3**

✅ **Decisão: LEAN_OVER**
**Recomendação**: SMALL via "book bypass" (cara é reserva → cap em SMALL)
✅ Aparece em Hot Picks como pick moderado

---

### Cenário E — Cara perfeitamente neutro 😐

**Quem é**: titular regular. Normalmente faz **12 pts em 28 min**.
**Como tá hoje**: 8 pts em 20 min, heat 0.05 (neutro). Q3 com 9 min.
**Linha do book**: 12.5

**O sistema pensa**:
- Ritmo normal: 12 / 28 = **0.43 pts/min**
- Ritmo agora: 8 / 20 = **0.40 pts/min** (praticamente igual)
- Nada de quente nem frio
- Mistura padrão: ritmo final ~0.41

**Conta**:
- 8 min restantes × 0.41 = +3.3 pts
- **Projeção final: 11.3**
- **Edge: 11.3 − 12.5 = -1.2**

⚪ **Decisão: NEUTRO**
❌ Não aparece em Hot Picks

---

### Cenário F — Cara em foul trouble 🚫

**Quem é**: pivô titular. Normalmente faz **15 pts em 30 min**.
**Como tá hoje**: 10 pts em 18 min, **5 fouls** (próximo de foul out), heat +0.30. Q3 com 5 min.
**Linha do book**: 14.5

**O sistema pensa**:
- 5 fouls com tempo restante = **risco gigante de foul out**
- Sistema corta minutos esperados (cara vai jogar bem menos)
- Sistema corta o ritmo também (cara vai jogar mais defensivamente)

**Conta**:
- Minutos totais esperados: 20.5 (em vez dos 30 normais)
- Resta jogar só ~2.5 min
- Ritmo cortado: 0.36 pts/min
- +0.9 pts
- **Projeção final: 10.9**
- **Edge: 10.9 − 14.5 = -3.6**

🔴 **Decisão: LEAN_UNDER**
**Razão mostrada**: "Foul trouble — minutos cortados"
✅ Aparece em Hot Picks

---

### Cenário G — Time perdendo de muito (blowout) 💥

**Quem é**: titular. Normalmente faz **18 pts em 32 min**.
**Como tá hoje**: 14 pts em 22 min. Time perdendo por 18 pts no Q4 (blowout iminente).
**Linha do book**: 18.5

**O sistema pensa**:
- Blowout severo → titulares vão descansar logo, reservas entram
- Sistema corta os minutos esperados em ~30%
- Cara mal vai voltar pra jogar

**Conta**:
- Minutos totais esperados: 23 (em vez de 32)
- Restam só ~1 min de quadra
- **Projeção final: 14.5**
- **Edge: 14.5 − 18.5 = -4.0**

🔴 **Decisão: STRONG_UNDER**
**Razão**: "Blowout — minutos restantes reduzidos"
✅ Aparece em Hot Picks

---

### Cenário H — Cara jogando além do padrão 🎯

**Quem é**: titular. Normalmente joga só **25 min e faz 14 pts**.
**Como tá hoje**: 16 pts em 28 min (já passou da minutagem dele), heat +0.55. Q4 com 8 min.
**Linha do book**: 22.5

**O sistema pensa**:
- nbarotations diz "ele deveria ter saído há 3 min" — mas claramente continua
- Sistema reconhece que **ele tá em jogo bom** + **quente** → coach vai mantê-lo
- Floor de minutos garante **4+ min adicionais**

**Conta**:
- Ritmo misturado ~0.60 pts/min (cara hot)
- 4 min × 0.60 = +2.4 pts
- Heat boost +18% no que falta
- **Projeção final: 19**
- **Edge: 19 − 22.5 = -3.5**

🔴 **Decisão: LEAN_UNDER**
✅ Aparece em Hot Picks (apesar de o cara estar quente, a linha do book tá ALTA demais)

---

### Cenário I — Hot pace mas com pouco tempo 🤔

**Quem é**: titular. Normalmente faz **12 pts em 28 min**.
**Como tá hoje**: 6 pts em **só 4 min**, heat +0.45. Q1 com 8 min ainda.
**Linha do book**: 12.5

**O sistema pensa**:
- 6 pts em 4 min é 1.5 pts/min — **3.5× o normal!**
- Mas com **apenas 4 minutos jogados**, isso pode ser sorte
- Sistema aplica "regressão pra média": puxa o ritmo pra perto do normal
- Reduz 50% do ritmo atual + 50% do normal

**Conta**:
- Ritmo regressado: ~0.96 pts/min (não 1.5)
- Restam ~24 min de jogo
- **Projeção raw**: alta, mas teto entra
- **Projeção final**: 12-13 (com teto)

⚪ **Decisão: NEUTRO ou LEAN_OVER fraco** (edge < +1.5)
**Razão**: "Ritmo inicial muito acima da média — projeção regrediu pra base histórica"
⚠ Sample pequena demais pra confiar = sistema é cauteloso

---

### Cenário J — Cara zerado no início 🚫

**Quem é**: titular. Normalmente faz **14 pts em 29 min**.
**Como tá hoje**: 0 pts em 5 min — não engrenou ainda. Q1 com 7 min.
**Linha do book**: 13.5

**O sistema pensa**:
- 0 pts + apenas 5 min jogados + cara que normalmente produz
- **Não dá pra projetar com confiança nada**
- Sistema mostra **"—"** (indeterminado)

✅ **Decisão: NEUTRO (forçado)**
**Recomendação**: PASS
**Razão**: "Aguardando jogador entrar em ritmo"
❌ NÃO aparece em Hot Picks

---

## Resumo executivo

### Quando o sistema mostra um pick em Hot Picks

Precisa passar TODOS estes filtros:
- ✅ Edge ≥ +1.5 (OVER) OU edge ≤ -2.0 (UNDER)
- ✅ Cara jogou ao menos 5 min (sample mínima)
- ✅ Ainda tem produção esperada (projeção ≠ atual)
- ✅ Confiança ≥ 40% — OU 30% com book agreeing
- ✅ Não é indeterminado / não é cara zerado com sample pequena

### Quando o sistema filtra (não aparece)

- ❌ Edge marginal (entre -1 e +1.5)
- ❌ Cara zerado com pouco tempo
- ❌ Cara quente mas só 4 min jogados (anti-ruído)
- ❌ Confiança muito baixa (< 30%) mesmo com book
- ❌ Recomendação calculou como PASS

### Como o sistema se protege

- **Anti-UNDER absurdo**: piso de 70% do ritmo histórico, sem cortes duplos
- **Anti-OVER falso**: regressão de cara quente cedo demais, teto de sanidade, indeterminado em 0 pts
- **Anti-edge marginal**: limites assimétricos (+1.5 OVER, -2 UNDER)
- **Anti-falso positivo**: book bypass exige edge real ≥ 2 + confiança ≥ 30%

---

## Como usar o debug (`?debug=1`)

Quando você adiciona `?debug=1` na URL, cada card mostra dois painéis:

### Painel "GATES HOT PICKS" — explica por que está/não está em Hot Picks

| Campo | Como ler |
|---|---|
| **bet_recommendation** | PASS = não vai pra Hot Picks. SMALL/MEDIUM/LARGE = vai |
| **bet_size** | 0.33 (SMALL), 0.66 (MEDIUM), 1.0 (LARGE) — fração da aposta cheia |
| **betting_confidence** | Confiança 0..1. < 0.30 = PASS sempre. 0.30-0.40 = só com book bypass |
| **edge (synthetic)** | Nosso modelo: projeção − linha calculada por nós |
| **edge (real)** | Book real: projeção − linha do Bet365 |
| **decision** | Decisão pela nossa linha |
| **real_decision** | Decisão pela linha do book (essa é a que importa pra Hot Picks) |

### Painel "PROJECTION BREAKDOWN" — explica como chegou no número

| Campo | Como ler |
|---|---|
| **prior_avg** | Média histórica blendada (peso 55/30/15) |
| **prior_rate** | Ritmo histórico (pts/min na média) |
| **current_rate** | Ritmo agora (pts/min no jogo atual) |
| **hot_ratio** | Quão acima/abaixo do normal está (1.0 = no ritmo, 2.0 = dobro) |
| **is_in_good_game** | True = já jogou mais que o normal |
| **is_hot_signal** | True = termômetro indica calor (heat ≥ 0.30) |
| **weight_cap** | Teto do peso do ao vivo (0.85, 0.90 ou 0.95) |
| **weight_current_final** | Quanto o ao vivo pesou na conta final |
| **is_in_deficit** | True = cara claramente abaixo do normal (frio detectado) |
| **period_weight** | Quanto o padrão do quarter pesou |
| **target_minutes_final** | Quantos minutos no total esperamos dele |
| **soft_floor_applied** | Quando o piso de 70% bindou (cara muito frio) |
| **heat_multiplier** | Bônus (>1.0) ou corte (<1.0) do termômetro |
| **sanity_cap** | Teto absoluto da projeção |
| **final_expected** | A projeção final, depois de tudo |

---

## Histórico de mudanças (mai/2026)

Refactor grande pra deixar o motor menos pessimista com HOT players + menos agressivo
em UNDER. Casos motivadores: LeVert travando em proj=17 com 17/16min, Holmgren projetando
2 pts em 15 min, LeBron edge -1.0 virando LEAN_UNDER marginal.

Mudanças aplicadas:

1. ✅ Pesos do "cara frio" reduzidos (0.85 → 0.65 multi-sinal)
2. ✅ Piso de 70% do ritmo histórico (anti UNDER absurdo)
3. ✅ Não aplica corte do termômetro quando cara já está em "deficit" (sem double-count)
4. ✅ Floor agressivo de minutos quando cara está estendido + quente
5. ✅ Teto do peso do ao vivo escala com heat + minutos (até 95%)
6. ✅ Bônus do termômetro mais agressivo (até +30%)
7. ✅ Padrão do quarter pesa menos quando cara está fora do padrão
8. ✅ Teto da projeção dinâmico (até 2.6× histórico em career night)
9. ✅ Book bypass (livro concordando → SMALL com confiança ≥ 30%)
10. ✅ Limites assimétricos: +1.5 OVER, -2 UNDER, -4 STRONG_UNDER
11. ✅ Painel debug com todos os intermediários (`?debug=1`)
12. ✅ Cache de odds 45s no Q3 (era 90s)
13. ✅ Indicador "linha atualizada há Xs" no card
