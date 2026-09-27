from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class RetrievalScope:
    """一次检索允许访问的唯一数据范围。"""

    tenant_id: UUID
    knowledge_base_id: UUID