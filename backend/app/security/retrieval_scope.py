from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class RetrievalScope:
    """后端授权生成的不可变范围；None 表示跨库模式，不表示无限制。"""

    tenant_id: UUID
    knowledge_base_id: UUID | None = None
    knowledge_base_ids: frozenset[UUID] = frozenset()
    knowledge_base_names: tuple[tuple[UUID, str], ...] = ()

    def __post_init__(self):
        ids = frozenset(self.knowledge_base_ids)
        if self.knowledge_base_id is not None:
            if ids and ids != {self.knowledge_base_id}:
                raise ValueError("单库范围不能包含其他知识库。")
            ids = frozenset({self.knowledge_base_id})
        object.__setattr__(self, "knowledge_base_ids", ids)
        object.__setattr__(self, "knowledge_base_names", tuple(self.knowledge_base_names))

    def knowledge_base_filter(self, column):
        # 空集合生成恒假的 IN 条件，绝不退化为全租户搜索。
        if self.knowledge_base_id is not None:
            return column == self.knowledge_base_id
        return column.in_(sorted(self.knowledge_base_ids, key=str))

    def contains(self, tenant_id, knowledge_base_id):
        try:
            return UUID(str(tenant_id)) == self.tenant_id and UUID(str(knowledge_base_id)) in self.knowledge_base_ids
        except (TypeError, ValueError):
            return False
