"""Durable attempts with an exclusive run lock and content-bound resume."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import threading

from .common import digest, encoded, load, now, private_directory, records, sync_directory, write_private


class UncertainAttempt(RuntimeError):
    pass


class Ledger:
    def __init__(self, directory, binding, *, interrupted_attempts=(), retry_preconnect=False, uncertain_resends=0):
        self.directory = private_directory(directory)
        self.binding = binding
        self.path = self.directory / "attempts.jsonl"
        self._mutex = threading.RLock()
        self._tasks = {}
        self._task_locks = {}
        self._events = []
        self._interrupted_attempts = {r["prior_attempt_id"]: dict(r) for r in interrupted_attempts}
        self._retry_preconnect = retry_preconnect
        if type(uncertain_resends) is not int or uncertain_resends not in {0,1}:
            raise ValueError("uncertain resend policy must be zero or one")
        self._uncertain_resends = uncertain_resends

    @contextmanager
    def locked(self):
        fd = os.open(self.directory / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            manifest = self.directory / "binding.json"
            if manifest.is_symlink() or self.path.is_symlink():
                raise ValueError("run binding and ledger cannot be symlinks")
            if manifest.exists():
                if load(manifest) != self.binding:
                    raise ValueError("run binding changed; choose a new output directory")
            else:
                write_private(manifest, self.binding)
            self.reload()
            if self._uncertain_resends:
                self.reconcile_interrupted()
            yield self
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def reload(self):
        """Refresh indexes only while the run lock is held, before worker dispatch."""
        with self._mutex:
            self._events = list(records(self.path)) if self.path.exists() else []
            self._tasks = {}
            for event in self._events:
                self._tasks.setdefault(event.get("task_id"), []).append(event)

    def events(self):
        with self._mutex:
            return [dict(event) for event in self._events]

    def reconcile_interrupted(self):
        """Under the exclusive run lock, seal crashed attempts before recovery."""
        finished = {e['attempt_id'] for e in self._events if e['event']=='finished'}
        for start in list(self._events):
            if start['event']!='started' or start['attempt_id']in finished:
                continue
            folder=self.directory/'attempts'/start['attempt_id']
            request=load(folder/'request.json')
            if digest(request)!=start['request_sha256']:
                raise ValueError('interrupted request binding differs')
            result=folder/'result.json'
            if not result.exists():
                write_private(result,dict(transport_success=False,complete=False,dispatch_state='uncertain',
                              error='prior process ended after the durable start boundary; automatic recovery retains possible duplicate billing'))
            else:
                load(result)  # An atomic completed result can precede its finished event.
            self.append(dict(event='finished',task_id=start['task_id'],attempt_id=start['attempt_id'],
                        request_sha256=start['request_sha256'],result_path=str(result.relative_to(self.directory)),
                        result_sha256=digest(result.read_bytes()),reconciled_after_interruption=True))

    def events_since(self, cursor):
        with self._mutex:
            return [dict(event) for event in self._events[cursor:]], len(self._events)

    def task_events(self, task_id):
        with self._mutex:
            return [dict(event) for event in self._tasks.get(task_id, [])]

    def append(self, event):
        with self._mutex:
            self._append(event)

    def _append(self, event):
        event = dict(event, recorded_at=now())
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "ab") as stream:
            stream.write(encoded(event))
            stream.flush()
            os.fsync(stream.fileno())
        sync_directory(self.directory)
        self._events.append(event)
        self._tasks.setdefault(event.get("task_id"), []).append(event)

    def execute(self, task_id, request, call, *, max_attempts=1, retry_failed=False, retry_http_statuses=(), before_start=None):
        """Never repeat a completed task, or automatically resend an uncertain attempt.

        A bounded explicit retry can repeat a completed failed transport. Model
        disagreement and poor retrieval never trigger automatic retries.
        """
        with self._mutex:
            task_lock = self._task_locks.setdefault(task_id, threading.Lock())
        # Identical concurrent requests share the retained result, not a send.
        with task_lock:
            return self._execute(task_id, request, call, max_attempts=max_attempts,
                                 retry_failed=retry_failed, retry_http_statuses=retry_http_statuses,
                                 before_start=before_start)

    def inspect(self, task_id, request, *, max_attempts, retry_failed, retry_http_statuses):
        """Read-only reuse decision; execute repeats it under the task lock."""
        request_hash = digest(request)
        task = self.task_events(task_id)
        if any(e.get("request_sha256") != request_hash for e in task):
            raise ValueError("task request changed during resume")
        starts = [e for e in task if e["event"] == "started"]
        finished = {e["attempt_id"]: e for e in task if e["event"] == "finished"}
        for start in starts:
            if start["attempt_id"] not in finished:
                raise UncertainAttempt(f"uncertain attempt for {task_id}; inspect retained evidence")
        if starts:
            last = finished[starts[-1]["attempt_id"]]
            result_path = self.directory / last["result_path"]
            raw = result_path.read_bytes()
            if digest(raw) != last["result_sha256"]:
                raise ValueError("saved task result binding differs")
            result = load(result_path)
            authorization = self._interrupted_attempts.get(starts[-1]["attempt_id"])
            from .interruption import credit_rejection
            named_repeat = bool(retry_failed and authorization == dict(
                task_id=task_id, prior_attempt_id=starts[-1]["attempt_id"],
                request_sha256=request_hash, prior_result_sha256=last["result_sha256"])
                and (result.get("dispatch_state") in {"uncertain", "not_sent"}
                    and result.get("complete") is False or credit_rejection(request, result)))
            from .recovery import known_not_sent_connect
            unsent_repeat = bool(retry_failed and self._retry_preconnect and known_not_sent_connect(result))
            uncertain_count = sum(load(self.directory / e['result_path']).get('dispatch_state')=='uncertain' for e in finished.values())
            automatic_repeat = bool(retry_failed and self._uncertain_resends and result.get('dispatch_state')=='uncertain' and uncertain_count <= self._uncertain_resends
                                    and not result.get('native_quota_evidence') and not result.get('fatal_configuration')
                                    and result.get('http_status')not in {401,402,403})
            if retry_failed and result.get("dispatch_state") == "uncertain" and not (named_repeat or automatic_repeat):
                if self._uncertain_resends:
                    return dict(dispatch=False,result=result)
                raise UncertainAttempt(f"uncertain transport for {task_id}; no covered automatic resend")
            if not (named_repeat or unsent_repeat or automatic_repeat) and not (retry_failed and result.get("transport_success") is False and
                    result.get("complete") is True and result.get("http_status") in retry_http_statuses):
                return dict(dispatch=False, result=result)
        if len(starts) >= max_attempts:
            return dict(dispatch=False, result=result)
        return dict(dispatch=True, attempt=len(starts) + 1,
                    previous=result if starts else None,
                    completed_at=last["recorded_at"] if starts else None)

    def _execute(self, task_id, request, call, *, max_attempts, retry_failed, retry_http_statuses, before_start):
        state = self.inspect(task_id, request, max_attempts=max_attempts,
                             retry_failed=retry_failed, retry_http_statuses=retry_http_statuses)
        if not state["dispatch"]:
            return state["result"]
        request_hash = digest(request)
        with self._mutex:
            # Reservation and the durable send boundary form one critical section.
            # Known-unsent rejections do not become uncertain started attempts.
            if before_start:
                before_start(task_id, request, state["attempt"])
            attempt_id = digest(dict(task_id=task_id, attempt=state["attempt"]))
            folder = private_directory(self.directory / "attempts" / attempt_id)
            request_path = folder / "request.json"
            if request_path.exists():
                if request_path.read_bytes() != encoded(request):
                    raise ValueError("orphan request bytes differ; inspect private attempt")
            else:
                write_private(request_path, request)
            # This fsync'd event is the spending boundary; no dispatch precedes it.
            self.append(dict(event="started", task_id=task_id, attempt_id=attempt_id,
                             request_sha256=request_hash))
        try:
            result = call(folder)
        except Exception as error:
            # Messages can contain URLs/headers/credentials. Preserve a neutral
            # failure class here; transports retain permitted response data.
            result = dict(transport_success=False, error_type=type(error).__name__,
                          dispatch_state="uncertain",
                          error="task failed; inspect private transport records")
        result_path = folder / "result.json"
        write_private(result_path, result)
        self.append(dict(event="finished", task_id=task_id, attempt_id=attempt_id,
                         request_sha256=request_hash,
                         result_path=str(result_path.relative_to(self.directory)),
                         result_sha256=digest(result_path.read_bytes())))
        return result
