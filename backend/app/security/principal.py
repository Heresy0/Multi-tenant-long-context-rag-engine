from dataclasses import dataclass
from uuid import UUID


@dataclass(
    frozen=True,
    slots=True,
)
class Principal:
    """已经通过身份认证的当前用户。"""

    user_id: UUID
    tenant_id: UUID
    external_subject: str