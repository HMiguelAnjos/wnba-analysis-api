"""
Ponto de entrada para o executável PyInstaller.

Uso direto:
    python run.py                  # porta padrão 8000
    PORT=9000 python run.py        # porta customizada
    python run.py --port 9000      # alternativa via argumento

O Electron usa esse executável em modo produção.
"""

import argparse
import os

import uvicorn


def _load_dotenv(path: str = ".env") -> None:
    """
    Carrega .env (KEY=VALUE) pro os.environ — sem dependência externa.
    Não sobrescreve variáveis já definidas no ambiente. Precisa rodar
    ANTES de importar a app (a config é lida no import).
    """
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="NBA Analysis API")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("PORT", "8000")),
        help="Porta em que a API vai escutar (padrão: 8000)",
    )
    parser.add_argument(
        "--host",
        type=str,
        default=os.getenv("HOST", "127.0.0.1"),
        help="Host/interface de rede (padrão: 127.0.0.1)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    _load_dotenv()  # carrega .env (LEAGUE_ID, DEFAULT_SEASON, etc.) antes da app
    args = parse_args()
    uvicorn.run(
        "src.main:app",
        host=args.host,
        port=args.port,
        log_level="info",
    )
