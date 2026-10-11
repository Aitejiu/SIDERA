from threading import Event, RLock
from uuid import uuid4

from sidera_agents.contracts import (
    CapabilitySupport,
    ConfigValidation,
    NativeCancelOutcome,
    NativeCloseOutcome,
    NativeHandle,
    NativeInspection,
    NativeOutcome,
    ProcessIdentity,
    RuntimeDescriptor,
    RuntimeProbe,
)


class ControlledHarness:
    """Owned native boundary only; no OS process or Provider is started."""

    harness_id = "controlled-product"
    executable_name = "python"

    def __init__(self):
        self.entered = Event()
        self.release = Event()
        self.release.set()
        self.finished = Event()
        self.closed = Event()
        self.lock = RLock()
        self.calls = []
        self.inspections = {}
        self.handles = {}
        self.close_unknown = False
        self.close_attempted = Event()

    def describe(self):
        return RuntimeDescriptor(
            harness_id=self.harness_id,
            display_name="Owned controlled product harness",
            fields=(),
            model_options=(),
            thinking_options=(),
        )

    def probe(self, candidate, checked_at):
        self.calls.append(("probe", candidate.executable))
        return RuntimeProbe(
            actual_version="controlled-1",
            protocol_state="ready",
            capabilities={
                name: CapabilitySupport(
                    status="supported", reason_code="controlled", constraints={}
                )
                for name in ("prompt", "acp", "model", "native_tools", "artifacts")
            },
            checked_at=checked_at,
        )

    def validate(self, profile, runtime):
        return ConfigValidation(valid=True, issues=(), effective_preview=None)

    def open(self, request):
        with self.lock:
            self.calls.append(("open", request.run_id))
            handle = NativeHandle(
                run_id=request.run_id,
                session_id="controlled-" + str(request.run_id),
                process_identity=ProcessIdentity(
                    pid=1,
                    start_identity="no-os-process-controlled-boundary",
                    owned_nonce=uuid4(),
                ),
                capabilities=("prompt", "acp", "native_tools"),
            )
            self.handles[request.run_id] = handle
            self.inspections[request.run_id] = NativeInspection(
                process_state="alive", response_state="active", reason_code=None
            )
            return handle

    def prompt(self, handle, input, response_id):
        with self.lock:
            self.calls.append(("prompt", handle.run_id, response_id))
        self.entered.set()
        if not self.release.wait(timeout=30):
            raise AssertionError(
                "Owned test did not release its controlled native call"
            )
        with self.lock:
            self.inspections[handle.run_id] = NativeInspection(
                process_state="alive", response_state="idle", reason_code=None
            )
        self.finished.set()
        return NativeOutcome(
            response_id=response_id,
            status="completed",
            reason_code=None,
            observed_model=None,
            usage=None,
        )

    def read_events(self, handle, after_cursor, limit=100):
        return ()

    def cancel(self, handle, response_id):
        self.calls.append(("cancel", handle.run_id, response_id))
        return NativeCancelOutcome(
            response_id=response_id,
            status="unknown",
            reason_code="controlled_cancel_unknown",
        )

    def inspect(self, handle):
        with self.lock:
            return self.inspections[handle.run_id]

    def close(self, handle):
        with self.lock:
            self.calls.append(("close", handle.run_id))
            self.close_attempted.set()
            if self.close_unknown:
                return NativeCloseOutcome(
                    status="unknown", reason_code="controlled_close_unknown"
                )
            self.inspections[handle.run_id] = NativeInspection(
                process_state="gone", response_state="idle", reason_code=None
            )
            self.closed.set()
            return NativeCloseOutcome(status="closed", reason_code=None)

    def observe_gone(self):
        with self.lock:
            self.close_unknown = False
            for run_id in self.handles:
                self.inspections[run_id] = NativeInspection(
                    process_state="gone", response_state="idle", reason_code=None
                )
            self.closed.set()


class ControlledSecretStore:
    def __init__(self):
        self.values = {}
        self.reads = []

    def put_new(self, ref, value):
        self.values[ref] = value

    def read(self, ref):
        self.reads.append(ref)
        return self.values[ref]

    def delete_owned(self, ref):
        self.values.pop(ref, None)
