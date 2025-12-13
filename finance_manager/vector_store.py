from __future__ import annotations

from typing import Iterable, List, Optional

try:
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, PointStruct, VectorParams
except Exception:  # pragma: no cover - qdrant is optional at runtime
    QdrantClient = None  # type: ignore
    PointStruct = dict  # type: ignore

from finance_manager.config import Settings
from finance_manager.logger import logger


class VectorStore:
    """Minimal Qdrant wrapper."""

    def __init__(self, settings: Settings):
        if QdrantClient is None:
            raise RuntimeError("qdrant-client is required for VectorStore")
        self.settings = settings
        self.client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
        self.collection = "finance-documents"
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        try:
            self.client.get_collection(self.collection)
        except Exception:
            self.client.recreate_collection(
                collection_name=self.collection,
                vectors_config=VectorParams(size=768, distance=Distance.COSINE),
            )

    def upsert_embeddings(self, embeddings: Iterable[PointStruct]) -> None:
        self.client.upsert(collection_name=self.collection, points=list(embeddings))

    def search(self, vector: List[float], limit: int = 5) -> List[dict]:
        res = self.client.search(collection_name=self.collection, query_vector=vector, limit=limit)
        return [hit.dict() for hit in res]


class NullVectorStore:
    """No-op vector store used when Qdrant is unavailable."""

    def __init__(self, *_: object, **__: object) -> None:
        self.collection = "noop"

    def upsert_embeddings(self, embeddings: Iterable[dict]) -> None:  # pragma: no cover - trivial
        return

    def search(self, vector: List[float], limit: int = 5) -> List[dict]:  # pragma: no cover - trivial
        return []


def get_vector_store(settings: Settings) -> VectorStore | NullVectorStore:
    try:
        return VectorStore(settings)
    except Exception as err:  # pragma: no cover - fallback path
        logger.info("using_null_vector_store", error=str(err))
        return NullVectorStore()


