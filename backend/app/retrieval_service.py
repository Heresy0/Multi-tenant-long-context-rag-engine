from threading import RLock
from pathlib import Path

from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever

from .config import Settings
from .rag import create_retriever, index_file as index_rag_file


class RetrievalService:
    """管理检索器的创建、查询和刷新。"""

    def __init__(self,settings: Settings) -> None:
        self._settings = settings
        self._lock = RLock()
        self._retriever: BaseRetriever | None = None

    def _build_retriever(self) -> BaseRetriever:
        return create_retriever(
            settings=self._settings,
            k=8,
            fetch_k=20,
        )

    def _get_retriever(self) -> BaseRetriever:
        with self._lock:
            if self._retriever is None:
                self._retriever = self._build_retriever()

            return self._retriever

    def search(self,query: str) -> list[Document]:
        retriever = self._get_retriever()

        #invoke放在锁外，允许多个查询并发执行
        return retriever.invoke(query)

    def index_file(self, file_path: str | Path) -> int:
        """入库单个文件，并在数据变化后刷新检索器。"""
        written_count = index_rag_file(
            file_path,
            settings=self._settings,
        )

        if written_count > 0:
            self.refresh()

        return written_count

    def refresh(self) -> None:
        #先构建成功，再替换旧检索器。
        #如果构建失败，旧检索器还能继续工作。
        new_retriever = self._build_retriever()

        with self._lock:
            self._retriever = new_retriever
