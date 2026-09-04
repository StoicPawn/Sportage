from __future__ import annotations

from abc import ABC, abstractmethod

from arbengine.models import Quote


class OddsProvider(ABC):
    @abstractmethod
    def fetch_quotes(self) -> list[Quote]:
        raise NotImplementedError
