import hashlib
from pathlib import Path

from langchain_community.document_loaders import (
    Docx2txtLoader,
    PyPDFLoader,
    TextLoader,
)
from langchain_core.documents import Document
from langchain_openai import OpenAIEmbeddings
from langchain_text_splitters import (
    RecursiveCharacterTextSplitter,
)

from .config import Settings
from .db.models import EMBEDDING_DIMENSION
from .document_splitter import split_docx


CHUNKING_VERSION = "structured-v1"


def split_file(
    file_path: str | Path,
    *,
    document_name: str | None = None,
) -> list[Document]:
    path = Path(file_path).resolve()

    if path.suffix.lower() == ".docx":
        return split_docx(
            path,
            document_name=document_name,
        )

    documents = load_file(path)
    splitter = create_text_splitter()
    chunks = splitter.split_documents(documents)

    if document_name is not None:
        for chunk in chunks:
            chunk.metadata["document_name"] = (
                document_name
            )

    return chunks


def create_embeddings(
    settings: Settings,
) -> OpenAIEmbeddings:
    """创建用于入库和查询的嵌入模型。"""
    return OpenAIEmbeddings(
        model=settings.embedding_model,
        openai_api_key=settings.chat_api_key,
        base_url=settings.chat_base_url,
        dimensions=EMBEDDING_DIMENSION,
        check_embedding_ctx_length=False,
        chunk_size=10,
    )


def load_file(
    file_path: str | Path,
) -> list[Document]:
    """根据文件扩展名选择加载器。"""
    path = Path(file_path).resolve()

    if not path.is_file():
        raise FileNotFoundError(
            f"文件不存在：{file_path}"
        )

    suffix = path.suffix.lower()

    if suffix == ".pdf":
        loader = PyPDFLoader(str(path))

    elif suffix == ".docx":
        loader = Docx2txtLoader(str(path))

    elif suffix in {".txt", ".md"}:
        loader = TextLoader(
            str(path),
            encoding="utf-8",
        )

    else:
        raise ValueError(
            f"暂不支持该文件类型：{suffix}"
        )

    documents = loader.load()

    for document in documents:
        document.metadata["source"] = str(path)

    return documents


def create_text_splitter(
) -> RecursiveCharacterTextSplitter:
    """创建适合中文企业文档的文本切分器。"""
    return RecursiveCharacterTextSplitter(
        separators=[
            "\n\n",
            "\n",
            "。",
            "！",
            "？",
            "；",
            "，",
            " ",
            "",
        ],
        chunk_size=500,
        chunk_overlap=50,
        add_start_index=True,
    )


def calculate_file_hash(
    file_path: str | Path,
) -> str:
    """计算文件内容的SHA-256。"""
    path = Path(file_path).resolve()
    hasher = hashlib.sha256()

    with path.open("rb") as file:
        while data := file.read(1024 * 1024):
            hasher.update(data)

    return hasher.hexdigest()


def calculate_source_id(
    file_path: str | Path,
) -> str:
    """根据正式文件路径生成稳定的文档来源标识。"""
    path = Path(file_path).resolve()
    return hashlib.sha256(
        str(path).lower().encode("utf-8")
    ).hexdigest()
