from datetime import datetime

from pydantic import BaseModel


class LivenessResponse(BaseModel):
    status: str


class ReadinessResponse(BaseModel):
    status: str
    database: str


class IndexingQueueStatus(BaseModel):
    queued: int
    running: int
    succeeded: int
    failed: int
    oldest_queued_seconds: float | None
    succeeded_last_24_hours: int
    failed_last_24_hours: int


class IndexingHealthResponse(BaseModel):
    status: str
    active_workers: int
    last_heartbeat_at: datetime | None
    queue: IndexingQueueStatus
