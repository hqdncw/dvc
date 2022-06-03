import hashlib
import locale
import logging
import os
from collections import defaultdict
from typing import (
    TYPE_CHECKING,
    Collection,
    Dict,
    Generator,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Set,
)

from funcy import cached_property, first
from kombu.message import Message

from dvc.daemon import daemonize
from dvc.exceptions import DvcException
from dvc.ui import ui

from ..exceptions import ExpQueueEmptyError, UnresolvedQueueExpNamesError
from ..executor.base import (
    EXEC_PID_DIR,
    EXEC_TMP_DIR,
    BaseExecutor,
    ExecutorInfo,
    ExecutorResult,
)
from ..executor.local import WorkspaceExecutor
from ..refs import EXEC_BRANCH
from ..stash import ExpStashEntry
from .base import BaseStashQueue, QueueDoneResult, QueueEntry, QueueGetResult
from .tasks import run_exp

if TYPE_CHECKING:
    from dvc.repo.experiments import Experiments
    from dvc_task.app import FSApp
    from dvc_task.proc.manager import ProcessManager
    from dvc_task.worker import TemporaryWorker

logger = logging.getLogger(__name__)


class _MessageEntry(NamedTuple):
    msg: Message
    entry: QueueEntry


class _TaskEntry(NamedTuple):
    task_id: str
    entry: QueueEntry


class LocalCeleryQueue(BaseStashQueue):
    """DVC experiment queue.

    Maps queued experiments to (Git) stash reflog entries.
    """

    CELERY_DIR = "celery"

    _shutdown_task_ids: Set[str] = set()

    @cached_property
    def wdir(self) -> str:
        return os.path.join(self.repo.tmp_dir, EXEC_TMP_DIR, self.CELERY_DIR)

    @cached_property
    def celery(self) -> "FSApp":
        from kombu.transport.filesystem import Channel  # type: ignore

        # related to https://github.com/iterative/dvc-task/issues/61
        Channel.QoS.restore_at_shutdown = False

        from dvc_task.app import FSApp

        app = FSApp(
            "dvc-exp-local",
            wdir=self.wdir,
            mkdir=True,
            include=[
                "dvc.repo.experiments.queue.tasks",
                "dvc_task.proc.tasks",
            ],
        )
        app.conf.update({"task_acks_late": True})
        return app

    @cached_property
    def proc(self) -> "ProcessManager":
        from dvc_task.proc.manager import ProcessManager

        pid_dir = os.path.join(self.repo.tmp_dir, EXEC_TMP_DIR, EXEC_PID_DIR)
        return ProcessManager(pid_dir)

    @cached_property
    def worker(self) -> "TemporaryWorker":
        from dvc_task.worker import TemporaryWorker

        # NOTE: Use thread pool with concurrency 1 and disabled prefetch.
        # Worker scaling should be handled by running additional workers,
        # rather than increasing pool concurrency.
        #
        # We use "threads" over "solo" (inline single-threaded) execution so
        # that we still have access to the control/broadcast API (which
        # requires a separate message handling thread in the worker).
        #
        # Disabled prefetch ensures that each worker will can only schedule and
        # execute up to one experiment at a time (and a worker cannot prefetch
        # additional experiments from the queue).
        return TemporaryWorker(
            self.celery,
            pool="threads",
            concurrency=1,
            prefetch_multiplier=1,
            without_heartbeat=True,
            without_mingle=True,
            without_gossip=True,
            timeout=10,
        )

    def spawn_worker(self):
        from dvc_task.proc.process import ManagedProcess

        logger.debug("Spawning exp queue worker")
        wdir_hash = hashlib.sha256(self.wdir.encode("utf-8")).hexdigest()[:6]
        node_name = f"dvc-exp-{wdir_hash}-1@localhost"
        cmd = ["exp", "queue-worker", node_name]
        if os.name == "nt":
            daemonize(cmd)
        else:
            ManagedProcess.spawn(
                ["dvc"] + cmd, wdir=self.wdir, name="dvc-exp-worker"
            )

    def put(self, *args, **kwargs) -> QueueEntry:
        """Stash an experiment and add it to the queue."""
        entry = self._stash_exp(*args, **kwargs)
        self.celery.signature(run_exp.s(entry.asdict())).delay()
        return entry

    # NOTE: Queue consumption should not be done directly. Celery worker(s)
    # will automatically consume available experiments.
    def get(self) -> QueueGetResult:
        raise NotImplementedError

    def _remove_revs(self, stash_revs: Mapping[str, ExpStashEntry]):
        try:
            for msg, queue_entry in self._iter_queued():
                if queue_entry.stash_rev in stash_revs:
                    self.celery.reject(msg.delivery_tag)
        finally:
            super()._remove_revs(stash_revs)

    def iter_queued(self) -> Generator[QueueEntry, None, None]:
        for _, entry in self._iter_queued():
            yield entry

    def _iter_queued(self) -> Generator[_MessageEntry, None, None]:
        for msg in self.celery.iter_queued():
            if msg.headers.get("task") != run_exp.name:
                continue
            args, kwargs, _embed = msg.decode()
            entry_dict = kwargs.get("entry_dict", args[0])
            yield _MessageEntry(msg, QueueEntry.from_dict(entry_dict))

    def _iter_processed(self) -> Generator[_MessageEntry, None, None]:
        for msg in self.celery.iter_processed():
            if msg.headers.get("task") != run_exp.name:
                continue
            args, kwargs, _embed = msg.decode()
            entry_dict = kwargs.get("entry_dict", args[0])
            yield _MessageEntry(msg, QueueEntry.from_dict(entry_dict))

    def _iter_active_tasks(self) -> Generator[_TaskEntry, None, None]:
        from celery.result import AsyncResult

        for msg, entry in self._iter_processed():
            task_id = msg.headers["id"]
            result: AsyncResult = AsyncResult(task_id)
            if not result.ready():
                yield _TaskEntry(task_id, entry)

    def _iter_done_tasks(self) -> Generator[_TaskEntry, None, None]:
        from celery.result import AsyncResult

        for msg, entry in self._iter_processed():
            task_id = msg.headers["id"]
            result: AsyncResult = AsyncResult(task_id)
            if result.ready():
                yield _TaskEntry(task_id, entry)

    def iter_active(self) -> Generator[QueueEntry, None, None]:
        for _, entry in self._iter_active_tasks():
            yield entry

    def iter_done(self) -> Generator[QueueDoneResult, None, None]:
        for _, entry in self._iter_done_tasks():
            yield QueueDoneResult(entry, self.get_result(entry))

    def reproduce(self) -> Mapping[str, Mapping[str, str]]:
        raise NotImplementedError

    def get_result(
        self, entry: QueueEntry, timeout: Optional[float] = None
    ) -> Optional[ExecutorResult]:
        from celery.exceptions import TimeoutError as _CeleryTimeout
        from celery.result import AsyncResult

        def _load_info(rev: str) -> ExecutorInfo:
            infofile = self.get_infofile_path(rev)
            return ExecutorInfo.load_json(infofile)

        try:
            executor_info = _load_info(entry.stash_rev)
            if executor_info.collected:
                return executor_info.result
        except FileNotFoundError:
            # Infofile will not be created until execution begins
            pass

        for queue_entry in self.iter_queued():
            if entry.stash_rev == queue_entry.stash_rev:
                raise DvcException("Experiment has not been started.")
        for task_id, active_entry in self._iter_active_tasks():
            if entry.stash_rev == active_entry.stash_rev:
                logger.debug("Waiting for exp task '%s' to complete", task_id)
                try:
                    result: AsyncResult = AsyncResult(task_id)
                    result.get(timeout=timeout)
                except _CeleryTimeout as exc:
                    raise DvcException(
                        "Timed out waiting for exp to finish."
                    ) from exc
                executor_info = _load_info(entry.stash_rev)
                return executor_info.result
        raise DvcException("Invalid experiment.")

    def kill(self, revs: Collection[str]) -> None:
        to_kill: Set[QueueEntry] = set()
        name_dict: Dict[
            str, Optional[QueueEntry]
        ] = self.match_queue_entry_by_name(set(revs), self.iter_active())

        missing_rev: List[str] = []
        for rev, queue_entry in name_dict.items():
            if queue_entry is None:
                missing_rev.append(rev)
            else:
                to_kill.add(queue_entry)

        if missing_rev:
            raise UnresolvedQueueExpNamesError(missing_rev)

        for queue_entry in to_kill:
            self.proc.kill(queue_entry.stash_rev)

    def shutdown(self, kill: bool = False):
        self.celery.control.shutdown()
        if kill:
            for _, task_entry in self._iter_active_tasks():
                try:
                    self.proc.kill(task_entry.stash_rev)
                except ProcessLookupError:
                    continue

    def follow(
        self,
        entry: QueueEntry,
        encoding: Optional[str] = None,
    ):
        for line in self.proc.follow(entry.stash_rev, encoding):
            ui.write(line, end="")

    def logs(
        self,
        rev: str,
        encoding: Optional[str] = None,
        follow: bool = False,
    ):
        queue_entry: Optional[QueueEntry] = self.match_queue_entry_by_name(
            {rev}, self.iter_active(), self.iter_done()
        ).get(rev)
        if queue_entry is None:
            if rev in self.match_queue_entry_by_name(
                {rev}, self.iter_queued()
            ):
                raise DvcException(
                    f"Experiment '{rev}' is in queue but has not been started"
                )
            raise UnresolvedQueueExpNamesError([rev])
        if follow:
            ui.write(
                f"Following logs for experiment '{rev}'. Use Ctrl+C to stop "
                "following logs (experiment execution will continue).\n"
            )
            try:
                self.follow(queue_entry)
            except KeyboardInterrupt:
                pass
            return
        try:
            proc_info = self.proc[queue_entry.stash_rev]
        except KeyError:
            raise DvcException(f"No output logs found for experiment '{rev}'")
        with open(
            proc_info.stdout,
            encoding=encoding or locale.getpreferredencoding(),
        ) as fobj:
            ui.write(fobj.read())


class WorkspaceQueue(BaseStashQueue):
    def put(self, *args, **kwargs) -> QueueEntry:
        return self._stash_exp(*args, **kwargs)

    def get(self) -> QueueGetResult:
        revs = self.stash.stash_revs
        if not revs:
            raise ExpQueueEmptyError("No experiments in the queue.")
        stash_rev, stash_entry = first(revs.items())
        entry = QueueEntry(
            self.repo.root_dir,
            self.scm.root_dir,
            self.ref,
            stash_rev,
            stash_entry.baseline_rev,
            stash_entry.branch,
            stash_entry.name,
            stash_entry.head_rev,
        )
        executor = self.setup_executor(self.repo.experiments, entry)
        return QueueGetResult(entry, executor)

    def iter_queued(self) -> Generator[QueueEntry, None, None]:
        for rev, entry in self.stash.stash_revs:
            yield QueueEntry(
                self.repo.root_dir,
                self.scm.root_dir,
                self.ref,
                rev,
                entry.baseline_rev,
                entry.branch,
                entry.name,
                entry.head_rev,
            )

    def iter_active(self) -> Generator[QueueEntry, None, None]:
        # Workspace run state is reflected in the workspace itself and does not
        # need to be handled via the queue
        raise NotImplementedError

    def iter_done(self) -> Generator[QueueDoneResult, None, None]:
        raise NotImplementedError

    def reproduce(self) -> Dict[str, Dict[str, str]]:
        results: Dict[str, Dict[str, str]] = defaultdict(dict)
        try:
            while True:
                entry, executor = self.get()
                results.update(self._reproduce_entry(entry, executor))
        except ExpQueueEmptyError:
            pass
        return results

    def _reproduce_entry(
        self, entry: QueueEntry, executor: BaseExecutor
    ) -> Dict[str, Dict[str, str]]:
        from dvc.stage.monitor import CheckpointKilledError

        results: Dict[str, Dict[str, str]] = defaultdict(dict)
        exec_name = "workspace"
        infofile = self.get_infofile_path(exec_name)
        try:
            rev = entry.stash_rev
            exec_result = executor.reproduce(
                info=executor.info,
                rev=rev,
                infofile=infofile,
                log_level=logger.getEffectiveLevel(),
                log_errors=not isinstance(executor, WorkspaceExecutor),
            )
            if not exec_result.exp_hash:
                raise DvcException(
                    f"Failed to reproduce experiment '{rev[:7]}'"
                )
            if exec_result.ref_info:
                results[rev].update(
                    self.collect_executor(
                        self.repo.experiments, executor, exec_result
                    )
                )
        except CheckpointKilledError:
            # Checkpoint errors have already been logged
            return {}
        except DvcException:
            raise
        except Exception as exc:
            raise DvcException(
                f"Failed to reproduce experiment '{rev[:7]}'"
            ) from exc
        finally:
            executor.cleanup()
        return results

    @staticmethod
    def collect_executor(  # pylint: disable=unused-argument
        exp: "Experiments",
        executor: BaseExecutor,
        exec_result: ExecutorResult,
    ) -> Dict[str, str]:
        results: Dict[str, str] = {}
        exp_rev = exp.scm.get_ref(EXEC_BRANCH)
        if exp_rev:
            assert exec_result.exp_hash
            logger.debug("Collected experiment '%s'.", exp_rev[:7])
            results[exp_rev] = exec_result.exp_hash
        return results

    def get_result(self, entry: QueueEntry) -> Optional[ExecutorResult]:
        raise NotImplementedError

    def kill(self, revs: Collection[str]) -> None:
        raise NotImplementedError

    def shutdown(self, kill: bool = False):
        raise NotImplementedError

    def logs(
        self,
        rev: str,
        encoding: Optional[str] = None,
        follow: bool = False,
    ):
        raise NotImplementedError