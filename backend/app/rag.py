import hashlib
# 文档对象
from langchain_core.documents import Document
# PDF、Word、纯文本加载器
from langchain_community.document_loaders import (PyPDFLoader,Docx2txtLoader,TextLoader,)
# 文本切分
from langchain_text_splitters import RecursiveCharacterTextSplitter
# 对话模型和嵌入模型
from langchain_openai import OpenAIEmbeddings
# 向量数据库
from langchain_chroma import Chroma
# 提示词和输出解析
from .config import Settings
from langchain_classic.chains import create_retrieval_chain
from langchain_classic.chains.combine_documents import (create_stuff_documents_chain,)
from langchain_core.retrievers import BaseRetriever
from pathlib import Path
from .prompts import RAGprompt
from .document_splitter import split_docx
from .hybrid_retriever import create_hybrid_retriever
from .reranker import QwenReranker
from .rerank_retriever import RerankRetriever


COLLECTION_NAME = "enterprise_knowledge"
CHUNKING_VERSION = "structured-v1"


def split_file(file_path: str | Path) -> list[Document]:
    path = Path(file_path).resolve()

    if path.suffix.lower() == ".docx":
        return split_docx(path)

    documents = load_file(path)
    splitter = create_text_splitter()
    return splitter.split_documents(documents)



def create_embeddings(settings: Settings) -> OpenAIEmbeddings:
    """创建用于入库和查询的嵌入模型。"""
    return OpenAIEmbeddings(
        model=settings.embedding_model,
        openai_api_key=settings.chat_api_key,
        base_url=settings.chat_base_url,
        check_embedding_ctx_length=False,
        chunk_size=10,
    )

def create_vector_store(settings: Settings, embeddings: OpenAIEmbeddings) -> Chroma:
    """打开或创建持久化的 Chroma 集合"""
    return Chroma(
        collection_name=COLLECTION_NAME,
        persist_directory=str(settings.chroma_directory),
        embedding_function=embeddings,
    )

def load_file(file_path: str | Path) -> list[Document]:
    """根据文件扩展名选择加载器。"""
    path = Path(file_path).resolve()

    if not path.is_file():
        raise FileNotFoundError(f"文件不存在：{file_path}")

    suffix = path.suffix.lower()

    if suffix ==".pdf":
        loader = PyPDFLoader(str(path))

    elif suffix == ".docx":
        loader = Docx2txtLoader(str(path))

    elif suffix in {".txt",".md"}:
        loader = TextLoader(str(path),encoding="utf-8")

    else: 
        raise ValueError(f"暂不支持该文件类型:{suffix}")

    documents = loader.load()

    # 统一使用绝对路径作为来源
    for document in documents:
        document.metadata["source"] = str(path)

    return documents


def create_text_splitter() -> RecursiveCharacterTextSplitter:
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
    """计算文件内容的 SHA-256。"""
    path = Path(file_path).resolve()
    hasher = hashlib.sha256()

    with path.open("rb") as file:
        while data:= file.read(1024*1024):
            hasher.update(data)

    return hasher.hexdigest()
        


def index_file(
        file_path: str | Path,
        settings: Settings | None = None,
) -> int:
    """加载，切分文件并写入 Chroma。
    返回本次实际写入的文档块数量。
    如果文件内容没有发生变化，则返回 0。
    """
    path = Path(file_path).resolve()

    if settings is None:
        settings = Settings()

    embeddings = create_embeddings(settings)
    vector_store = create_vector_store(
        settings = settings,
        embeddings =embeddings,
        )

    chunks = split_file(path)

    if not chunks: 
        raise ValueError(
            f"文档没有可索引的内容：{path}"
        )

    source = str(path)

    #用路径标识逻辑文档
    source_id = hashlib.sha256(
        source.lower().encode("utf-8")
    ).hexdigest()

    #用文件内容标识文档版本
    file_hash = calculate_file_hash(path)

    chunk_ids: list[str] = []

    for index, chunk in enumerate(chunks):

        chunk_content_hash = hashlib.sha256(
            chunk.page_content.encode("utf-8")
        ).hexdigest()

        parent_key = (
            chunk.metadata.get("parent_key",index)
        )

        parent_id = hashlib.sha256(
            (
                f"{source_id}:"
                f"{file_hash}:"
                f"{parent_key}"
            ).encode("utf-8")
        ).hexdigest()
        
        chunk_id = hashlib.sha256(
            (
                f"{source_id}:"
                f"{file_hash}:"
                f"{CHUNKING_VERSION}:"
                f"{index}"
                f"{chunk_content_hash}"
            ).encode("utf-8")
        ).hexdigest()

        chunk_ids.append(chunk_id)

        chunk.metadata.update({
            "source": source,
            "source_id": source_id,
            "file_hash": file_hash,
            "chunk_index": index,
            "chunk_content_hash": chunk_content_hash,
            "chunking_version": CHUNKING_VERSION,
            "parent_id": parent_id,
        })
    #查询当前文件已经存在的多有文档块
    existing = vector_store.get(
        where={"source_id":source_id,},
        include=["metadatas"],
    )

    existing_ids = set(existing.get("ids",[]))
    new_ids = set(chunk_ids)

    #文件内容合切片结果均没有变化
    if existing_ids == new_ids:
        return 0

    #先写入新版本
    #想同ID会执行upsert,不会产生重复数据  
    vector_store.add_documents(documents=chunks,ids=chunk_ids,)

    #新版本成功后，删除不属于当前版本的旧块
    stale_ids = list(existing_ids - new_ids)

    if stale_ids:
        vector_store.delete(
            ids=stale_ids
        )

    return len(chunks)


def create_retriever(
        settings: Settings | None = None,
        *,
        k: int = 8,
        fetch_k: int = 20,
) -> BaseRetriever:
    """创建企业知识库检索器。"""

    if k < 1:
        raise ValueError("k必须大于0")

    if fetch_k < k:
        raise ValueError("fetch_k不能小于k")

    if settings is None:
        settings = Settings()

    embeddings = create_embeddings(settings)

    vector_store = create_vector_store(
        settings = settings,
        embeddings = embeddings,
        )

    # 混合检索先返回 fetch_k 个候选，而不是最终 k 个结果
    hybrid_retriever = create_hybrid_retriever(
        vector_store,
        k=fetch_k,
        fetch_k=fetch_k,
    )

    reranker = QwenReranker(
        endpoint=settings.rerank_url,
        api_key=settings.chat_api_key,
        model=settings.rerank_model,
        timeout_seconds=settings.rerank_timeout_seconds,
    )

    return RerankRetriever(
        base_retriever=hybrid_retriever,
        reranker=reranker,
        top_n=k,
        search_kwargs={
            "k": k,
            "fetch_k": fetch_k,
        },
)


