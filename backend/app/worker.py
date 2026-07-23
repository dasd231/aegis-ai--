from __future__ import annotations

import asyncio
import logging
import signal

from app.core.config import get_settings
from app.services.job_queue import run_worker
from app.services.multimodal import close_multimodal_service
from app.services.vector_store import close_vector_store


async def main() -> None:
    if get_settings().ingestion_mode != "queue":
        raise RuntimeError("worker requires INGESTION_MODE=queue")
    loop = asyncio.get_running_loop()
    stop_requested = asyncio.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signum, stop_requested.set)
        except (NotImplementedError, RuntimeError):
            pass
    worker_task = asyncio.create_task(run_worker())
    stop_task = asyncio.create_task(stop_requested.wait())
    try:
        done, _ = await asyncio.wait({worker_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if stop_task in done and not worker_task.done():
            logging.getLogger(__name__).info("ingestion worker shutdown requested")
            worker_task.cancel()
        await worker_task
    finally:
        stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)
        await close_multimodal_service()
        await close_vector_store()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(main())
