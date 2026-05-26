"""
Baixa fixtures reais da NBA Live API pra testar offline.

Uso:
    python scripts/fetch_fixtures.py

Salva em tests/fixtures/*.json (ignorado pelo git). Depois pode rodar
o backend em modo offline:

    USE_FIXTURES=1 uvicorn src.main:app --reload

E o front conecta nele normal.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import requests

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures"

# Lista de fixtures a baixar. (filename, url, descrição).
FIXTURES = [
    (
        "scoreboard_today.json",
        "https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json",
        "Scoreboard atual (jogos de hoje)",
    ),
    (
        "boxscore_blowout_final.json",
        "https://cdn.nba.com/static/json/liveData/boxscore/boxscore_0042300405.json",
        "Boxscore Celtics x Mavs Finals G5 (decidiu série, blowout 39 pts)",
    ),
    (
        "boxscore_moderate_blowout.json",
        "https://cdn.nba.com/static/json/liveData/boxscore/boxscore_0042300401.json",
        "Boxscore Finals G1 (blowout moderado, 18 pts)",
    ),
]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"Salvando fixtures em {OUT}\n")

    failed = 0
    for filename, url, description in FIXTURES:
        print(f"→ {filename}")
        print(f"  {description}")
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            # Valida que é JSON antes de salvar
            json.loads(r.text)
            (OUT / filename).write_text(r.text, encoding="utf-8")
            print(f"  ✓ {len(r.content):,} bytes\n")
        except Exception as exc:
            print(f"  ✗ ERRO: {exc}\n")
            failed += 1

    if failed:
        print(f"⚠️ {failed} fixture(s) falharam")
        return 1

    print(f"✓ {len(FIXTURES)} fixtures salvas")
    print("\nPra rodar o backend usando elas:")
    print("  USE_FIXTURES=1 uvicorn src.main:app --reload")
    return 0


if __name__ == "__main__":
    sys.exit(main())
