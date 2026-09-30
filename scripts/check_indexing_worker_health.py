import os
import socket
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.config import Settings  # noqa: E402
from backend.app.db.session import (  # noqa: E402
    create_database_engine,
    create_session_factory,
)
from backend.app.worker_heartbeat_service import (  # noqa: E402
    WorkerHeartbeatService,
)


def main() -> int:
    settings = Settings()
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)

    try:
        worker_id = (
            os.getenv("INDEXING_WORKER_ID", "").strip()
            or socket.gethostname()
        )

        with session_factory() as session:
            active = WorkerHeartbeatService(
                session=session
            ).list_active(
                stale_after_seconds=(
                    settings.indexing_worker_stale_seconds
                )
            )
        return (
            0
            if any(
                worker.worker_id == worker_id
                for worker in active
            )
            else 1
        )
    except Exception:
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
