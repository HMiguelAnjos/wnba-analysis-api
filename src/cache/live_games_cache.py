"""
Cache abstraction for live games snapshot.

InMemoryLiveGamesCache is the default implementation.
To swap to Redis, implement AbstractLiveGamesCache and inject it
in main.py — no route changes required.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from src.schemas.live_schemas import TodayGamesSchema


@dataclass
class LiveGamesSnapshot:
    data: TodayGamesSchema
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def age_ms(self) -> int:
        delta = datetime.now(timezone.utc) - self.updated_at
        return max(0, int(delta.total_seconds() * 1000))


class AbstractLiveGamesCache(ABC):
    """
    Interface for live games cache.
    Swap implementations without touching routes.
    """

    @abstractmethod
    def get_snapshot(self) -> Optional[LiveGamesSnapshot]:
        """Return the latest snapshot, or None if not yet populated."""
        ...

    @abstractmethod
    def set_snapshot(self, data: TodayGamesSchema) -> None:
        """Store a new snapshot with the current timestamp."""
        ...


class InMemoryLiveGamesCache(AbstractLiveGamesCache):
    """Thread-safe in-memory implementation. Suitable for single-process deployments."""

    def __init__(self) -> None:
        self._snapshot: Optional[LiveGamesSnapshot] = None

    def get_snapshot(self) -> Optional[LiveGamesSnapshot]:
        return self._snapshot

    def set_snapshot(self, data: TodayGamesSchema) -> None:
        self._snapshot = LiveGamesSnapshot(data=data)
