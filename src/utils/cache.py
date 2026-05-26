import json
import logging
import os
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


class SimpleCache:
    """
    In-memory key-value cache com TTL por entrada.

    API pública
    -----------
    set(key, value, ttl)  – grava / atualiza
    get(key)              – retorna valor ou None se expirado/ausente
    has(key)              – True se a chave existe e não expirou
    invalidate(key)       – remove manualmente
    clear()               – limpa tudo
    count_prefix(prefix)  – conta entradas válidas com determinado prefixo
    status()              – diagnóstico resumido
    """

    def __init__(self) -> None:
        self._store: dict[str, tuple[Any, float]] = {}

    def get(self, key: str) -> Optional[Any]:
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expiry = entry
        if time.monotonic() > expiry:
            del self._store[key]
            return None
        return value

    def set(self, key: str, value: Any, ttl: int) -> None:
        self._store[key] = (value, time.monotonic() + ttl)

    def has(self, key: str) -> bool:
        """True se a chave existe e ainda não expirou."""
        return self.get(key) is not None

    def invalidate(self, key: str) -> None:
        self._store.pop(key, None)

    def clear(self) -> None:
        """Remove todas as entradas."""
        self._store.clear()

    def count_prefix(self, prefix: str) -> int:
        """Conta entradas válidas cujo nome começa com *prefix*."""
        now = time.monotonic()
        return sum(
            1
            for k, (_, exp) in list(self._store.items())
            if k.startswith(prefix) and exp > now
        )

    def status(self) -> dict:
        now = time.monotonic()
        valid_keys = [k for k, (_, exp) in list(self._store.items()) if exp > now]
        return {"total_entries": len(valid_keys), "keys": valid_keys}


# Alias para retrocompatibilidade
LocalCacheService = SimpleCache


class PersistentCache(SimpleCache):
    """
    SimpleCache com fallback em disco (JSON).

    Ao fazer `set`, grava também em *path* no disco.
    Ao fazer `get` com miss na memória, tenta carregar do disco.

    Isso garante que médias de temporada sobrevivam a restarts do
    container sem precisar chamar stats.nba.com novamente.

    TTL é armazenado como timestamp Unix absoluto no JSON, então
    funciona corretamente entre processos.

    Path:
      - Por padrão lê `CACHE_DIR` (env, default `/tmp`).
      - `/tmp` é efêmero no Railway → cache wipa todo deploy → custa
        ScraperAPI no próximo warm. Apontar pra um volume persistente
        (`CACHE_DIR=/data` + Railway Volume mountado em /data) elimina
        esse desperdício.
    """

    def __init__(self, path: Optional[str] = None) -> None:
        super().__init__()
        if path is None:
            from src.config import CACHE_DIR
            path = os.path.join(CACHE_DIR, "nba_season_cache.json")
        self._path = path
        self._disk: dict[str, tuple[Any, float]] = {}
        # Garante que o diretório existe (importante quando CACHE_DIR aponta
        # pra um volume montado que pode não ter sido inicializado ainda).
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        except OSError as exc:
            logger.warning("PersistentCache: não consegui criar diretório %s: %s",
                           os.path.dirname(self._path), exc)
        self._load_disk()

    def _load_disk(self) -> None:
        if not os.path.exists(self._path):
            logger.info(
                "PersistentCache: arquivo %s não existe (cache vazio — primeiro start ou volume não-persistente).",
                self._path,
            )
            return
        try:
            with open(self._path, "r") as f:
                raw = json.load(f)
            now = time.time()
            valid = {k: (v, exp) for k, (v, exp) in raw.items() if exp > now}
            stale = len(raw) - len(valid)
            self._disk = valid
            logger.info(
                "PersistentCache: carregou %d entradas válidas de %s%s",
                len(self._disk), self._path,
                f" (descartadas {stale} expiradas)" if stale > 0 else "",
            )
            # Promove pra memória todas as entradas válidas (evita 1 read
            # extra do disco no primeiro acesso de cada chave).
            for key, (value, expiry) in self._disk.items():
                remaining = int(expiry - now)
                if remaining > 0:
                    super().set(key, value, remaining)
        except Exception as exc:
            logger.warning("PersistentCache: falha ao carregar %s: %s", self._path, exc)
            self._disk = {}

    def _save_disk(self) -> None:
        try:
            # Snapshot ANTES de serializar: o live analysis grava em paralelo
            # (várias threads). Iterar o dict vivo causava
            # "dictionary changed size during iteration". Escreve atômico
            # (tmp → replace) pra não corromper o arquivo.
            snapshot = dict(self._disk)
            tmp = self._path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(snapshot, f)
            os.replace(tmp, self._path)
        except Exception as exc:
            logger.debug("PersistentCache: disk save pulado (%s)", exc)

    def get(self, key: str) -> Optional[Any]:
        # 1. memória
        value = super().get(key)
        if value is not None:
            return value
        # 2. disco
        entry = self._disk.get(key)
        if entry is None:
            return None
        value, expiry = entry
        if time.time() > expiry:
            del self._disk[key]
            return None
        # promove para memória (TTL restante)
        remaining = int(expiry - time.time())
        if remaining > 0:
            super().set(key, value, remaining)
        return value

    def set(self, key: str, value: Any, ttl: int) -> None:
        super().set(key, value, ttl)
        # Só persiste em disco valores JSON-serializáveis. Schemas Pydantic
        # e dataclasses (TeamBoardSchema, HotBoardSchema, MatchupContext...)
        # ficam SÓ em memória — evita o spam de "not JSON serializable" e o
        # risco de carregar dict no lugar do objeto após restart. A memória
        # já garante o cache dentro da sessão (TTL normal).
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            self._disk.pop(key, None)
            return
        self._disk[key] = (value, time.time() + ttl)
        self._save_disk()
