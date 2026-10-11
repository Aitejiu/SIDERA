from __future__ import annotations

from collections.abc import Callable
from threading import Condition, Thread, current_thread
from time import monotonic
from uuid import UUID


class NativeWorkers:
    def __init__(self) -> None:
        self._condition = Condition()
        self._threads: dict[UUID, Thread] = {}
        self._closing = False

    def dispatch(self, run_id: UUID, work: Callable[[], None]) -> bool:
        def run() -> None:
            failed = False
            try:
                work()
            except Exception:  # noqa: BLE001 - only a safe failure may reach the thread hook
                failed = True
            finally:
                with self._condition:
                    self._threads.pop(run_id, None)
                    self._condition.notify_all()
            if failed:
                # Leave the handler before raising so no raw exception is chained.
                raise RuntimeError("native_worker_failed") from None

        with self._condition:
            if self._closing or run_id in self._threads:
                return False
            thread = Thread(target=run, name=f"sidera-native-{run_id}", daemon=True)
            self._threads[run_id] = thread
            try:
                thread.start()
            except Exception:
                self._threads.pop(run_id, None)
                self._condition.notify_all()
                raise
            return True

    def busy(self, run_id: UUID) -> bool:
        with self._condition:
            return run_id in self._threads

    def run_ids(self) -> tuple[UUID, ...]:
        with self._condition:
            return tuple(self._threads)

    def begin_close(self) -> None:
        with self._condition:
            self._closing = True
            self._condition.notify_all()

    def wait_until(self, deadline: float) -> bool:
        while True:
            with self._condition:
                threads = tuple(self._threads.values())
            if not threads:
                return True
            if current_thread() in threads:
                return False
            for thread in threads:
                thread.join(timeout=max(0.0, deadline - monotonic()))
            with self._condition:
                if not self._threads:
                    return True
            if monotonic() >= deadline:
                return False
