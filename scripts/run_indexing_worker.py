import logging
import os
import signal
import socket
import sys
from pathlib import Path
from threading import Event, Thread
from time import monotonic

from prometheus_client import start_http_server


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.config import Settings  # noqa: E402
from backend.app.db.session import (  # noqa: E402
    create_database_engine,
    create_session_factory,
    verify_database_connection,
)
from backend.app.indexing.document_indexing_worker import (  # noqa: E402
    DocumentIndexingWorker,
)
from backend.app.monitoring.observability import event_message  # noqa: E402
from backend.app.monitoring.metrics import (  # noqa: E402
    record_cleaned_candidates,
    record_recovered_jobs,
)
from backend.app.indexing.worker_heartbeat_service import (  # noqa: E402
    WorkerHeartbeatService,
)
from backend.app.knowledge.keyword_repository import verify_keyword_index  # noqa: E402


logger = logging.getLogger("backend.app.indexing_worker")


def _worker_id() -> str:
    configured = os.getenv(
        "INDEXING_WORKER_ID",
        "",
    ).strip()

    if configured:
        return configured

    return socket.gethostname()


def _heartbeat_loop(
    *,
    stop_event: Event,
    session_factory,
    worker_id: str,
    interval_seconds: float,
) -> None:
    while not stop_event.wait(interval_seconds):
        try:
            with session_factory() as session:
                WorkerHeartbeatService(
                    session=session
                ).heartbeat(worker_id=worker_id)
        except Exception as exc:
            logger.exception(
                event_message(
                    "indexing.worker.heartbeat_failed",
                    worker_id=worker_id,
                    error_type=type(exc).__name__,
                )
            )


def main() -> None:
    settings = Settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(message)s",
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
    verify_keyword_index(engine)
    metrics_server, metrics_thread = start_http_server(
        settings.indexing_worker_metrics_port,
        addr="0.0.0.0",
    )
    worker_id = _worker_id()
    heartbeat_interval = (
        settings.indexing_worker_heartbeat_seconds
    )
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

    with session_factory() as session:
        WorkerHeartbeatService(
            session=session
        ).register(worker_id=worker_id)

    heartbeat_thread = Thread(
        target=_heartbeat_loop,
        kwargs={
            "stop_event": stop_event,
            "session_factory": session_factory,
            "worker_id": worker_id,
            "interval_seconds": heartbeat_interval,
        },
        name="indexing-worker-heartbeat",
        daemon=True,
    )
    heartbeat_thread.start()

    logger.info(
        event_message(
            "indexing.worker.started",
            worker_id=worker_id,
            heartbeat_seconds=heartbeat_interval,
            metrics_port=(
                settings.indexing_worker_metrics_port
            ),
        )
    )

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
                        record_recovered_jobs(recovered)
                        logger.warning(
                            event_message(
                                "indexing.job.recovered",
                                worker_id=worker_id,
                                count=recovered,
                            )
                        )

                    cleaned = (
                        worker
                        .cleanup_expired_failed_candidates()
                    )

                    if cleaned:
                        record_cleaned_candidates(cleaned)
                        logger.info(
                            event_message(
                                "indexing.candidate.cleaned",
                                worker_id=worker_id,
                                count=cleaned,
                            )
                        )

                processed = worker.run_once()

            if not processed:
                stop_event.wait(poll_seconds)

    finally:
        stop_event.set()
        heartbeat_thread.join(
            timeout=heartbeat_interval + 5
        )
        metrics_server.shutdown()
        metrics_server.server_close()
        metrics_thread.join(timeout=5)

        try:
            with session_factory() as session:
                WorkerHeartbeatService(
                    session=session
                ).mark_stopped(worker_id=worker_id)
        except Exception as exc:
            logger.exception(
                event_message(
                    "indexing.worker.stop_failed",
                    worker_id=worker_id,
                    error_type=type(exc).__name__,
                )
            )

        engine.dispose()
        logger.info(
            event_message(
                "indexing.worker.stopped",
                worker_id=worker_id,
            )
        )


if __name__ == "__main__":
    main()
