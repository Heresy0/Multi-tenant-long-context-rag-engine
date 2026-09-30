import logging
import signal
import sys
from pathlib import Path
from threading import Event
from time import monotonic


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.config import Settings  # noqa: E402
from backend.app.db.session import (  # noqa: E402
    create_database_engine,
    create_session_factory,
    verify_database_connection,
)
from backend.app.document_indexing_worker import (  # noqa: E402
    DocumentIndexingWorker,
)


logger = logging.getLogger("backend.app.indexing_worker")


def main() -> None:
    settings = Settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    stop_event = Event()

    def request_stop(
        _signal_number,
        _frame,
    ) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)

    engine = create_database_engine(
        settings.database_url
    )
    session_factory = create_session_factory(engine)
    verify_database_connection(engine)
    poll_seconds = max(
        settings.indexing_worker_poll_seconds,
        0.1,
    )
    recovery_interval = max(
        min(
            settings.indexing_job_stale_after_seconds
            / 2,
            60,
        ),
        5,
    )
    last_recovery = float("-inf")

    logger.info("文档索引 Worker 已启动")

    try:
        while not stop_event.is_set():
            with session_factory() as session:
                worker = DocumentIndexingWorker(
                    session=session,
                    settings=settings,
                )

                if (
                    monotonic() - last_recovery
                    >= recovery_interval
                ):
                    recovered = worker.recover_stale_jobs()
                    last_recovery = monotonic()

                    if recovered:
                        logger.warning(
                            "已恢复超时索引任务：count=%s",
                            recovered,
                        )

                processed = worker.run_once()

            if not processed:
                stop_event.wait(poll_seconds)

    finally:
        engine.dispose()
        logger.info("文档索引 Worker 已停止")


if __name__ == "__main__":
    main()
