"""Single indexing executor and document workflow, composed from public ports."""

import logging
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from threading import Lock, Timer

from app.modules.knowledge.public import ChunkWrite, DocumentNotFound, IndexStateConflict, Knowledge
from app.modules.retrieval.public import (
    IndexChunk, IndexWriteError, Retrieval, RetrievalUnavailable, index_representatives,
)

from .gate import Gate, Lease, Operation, State


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IndexResult:
    document_id: int
    outcome: str
    error: str | None = None


def _reason(stage, failure):
    detail = type(failure).__name__
    if isinstance(failure, RetrievalUnavailable):
        detail += f' ({failure.component}/{failure.cause})'
    elif isinstance(failure, IndexWriteError):
        detail += f' ({failure.cause}; cleanup={failure.cleanup_error})'
    return f'{stage}: {detail}'


class Indexer:
    def __init__(self, knowledge: Knowledge, retrieval: Retrieval, gate: Gate):
        self._knowledge, self._retrieval, self._gate = knowledge, retrieval, gate

    @property
    def ready_for_retry(self) -> bool:
        # A scheduling hint only; run must still acquire its own lease.
        return self._gate.state == State.READY

    def run(self, document_id: int, lease: Lease | None = None) -> IndexResult:
        if lease is None:
            lease = self._gate.try_acquire(Operation.MUTATION).lease
            if lease is None:
                return IndexResult(document_id, 'BUSY')
        elif not self._gate.owns(lease, Operation.MUTATION):
            raise ValueError('indexing requires this gate\'s active mutation lease')
        known_pending, stage = False, 'LOAD_DOCUMENT'
        with lease:
            try:
                try:
                    document = self._knowledge.get(document_id)
                except DocumentNotFound:
                    lease.confirm_completion()
                    return IndexResult(document_id, 'SKIPPED')
                if document.index_status == 'INDEXING':
                    raise IndexStateConflict('orphan indexing state requires offline recovery')
                if document.index_status != 'PENDING':
                    lease.confirm_completion()
                    return IndexResult(document_id, 'SKIPPED')
                known_pending = True
                stage = 'SPLIT'
                drafts = self._retrieval.split(document.content, document.title)
                writes = tuple(ChunkWrite(seq, draft.text, draft.byte_start, draft.byte_end,
                                          draft.heading_path, draft.token_count) for seq, draft in enumerate(drafts))
                stage = 'SAVE_CHUNKS'
                chunks = self._knowledge.begin_indexing(document_id, writes, expected_content=document.content)
                inputs = index_representatives(
                    IndexChunk(chunk.id, chunk.text, chunk.heading_path, document.tags) for chunk in chunks)
                stage = 'REPLACE_INDEX'
                if self._retrieval.replace(document_id, inputs) != len(inputs):
                    raise RuntimeError('indexed count mismatch')
                stage = 'MARK_INDEXED'
                self._knowledge.mark_indexed(document_id)
                lease.confirm_completion()
                return IndexResult(document_id, 'INDEXED')
            except Exception as failure:
                reason = _reason(stage, failure)
                cleanup_confirmed = terminal_confirmed = False
                if known_pending:
                    try:
                        self._retrieval.delete_document(document_id)
                        cleanup_confirmed = True
                    except Exception as cleanup_failure:
                        reason += '; ' + _reason('DELETE_INDEX', cleanup_failure)
                    try:
                        self._knowledge.mark_failed(document_id, reason[:1024])
                        terminal_confirmed = True
                    except Exception as terminal_failure:
                        logger.warning('indexing_terminal_unconfirmed document_id=%s cause=%s',
                                       document_id, type(terminal_failure).__name__)
                if cleanup_confirmed and terminal_confirmed:
                    lease.confirm_completion()
                logger.warning('indexing_failed document_id=%s stage=%s cause=%s cleanup_confirmed=%s terminal_confirmed=%s',
                               document_id, stage, type(failure).__name__, cleanup_confirmed, terminal_confirmed)
                return IndexResult(document_id, 'FAILED', reason)


class QueueClosed(RuntimeError):
    pass


class IndexingQueue:
    def __init__(self, indexer: Indexer):
        self._indexer = indexer
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='knowledge-index')
        self._lock = Lock()
        self._accepting = True
        self._pending: set[int] = set()
        self._retry_timer: Timer | None = None

    def submit(self, document_id: int, lease: Lease | None = None) -> Future:
        """Return the first attempt; BUSY unleased work remains owned by this queue."""
        with self._lock:
            if not self._accepting:
                raise QueueClosed('indexing executor has stopped accepting tasks')
            return self._executor.submit(self._run, document_id, lease)

    def _run(self, document_id: int, lease: Lease | None) -> IndexResult:
        result = self._indexer.run(document_id, lease)
        if lease is None and result.outcome == 'BUSY':
            with self._lock:
                if self._accepting:
                    self._pending.add(document_id)
                    self._schedule_retry()
        return result

    def _schedule_retry(self):
        # Called with the queue lock held. Waiting never occupies the index worker,
        # which may need to execute a later task holding the current mutation lease.
        if self._retry_timer is None:
            self._retry_timer = Timer(.25, self._retry_pending)
            self._retry_timer.daemon = True
            self._retry_timer.start()

    def _retry_pending(self):
        with self._lock:
            self._retry_timer = None
            if not self._accepting:
                return
            if self._indexer.ready_for_retry:
                for document_id in self._pending:
                    self._executor.submit(self._run, document_id, None)
                self._pending.clear()
            else:
                # Recovery is still explicit; inspect only the in-memory gate.
                self._schedule_retry()

    def close(self):
        with self._lock:
            self._accepting = False
            timer, self._retry_timer = self._retry_timer, None
            self._pending.clear()
            if timer is not None:
                timer.cancel()
        if timer is not None:
            timer.join()
        self._executor.shutdown(wait=True)
