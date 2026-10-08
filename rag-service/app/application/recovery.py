"""Consistency and offline rebuild workflows over public A/B snapshots."""

import logging
from dataclasses import dataclass

from app.modules.knowledge.public import ChunkWrite, Knowledge
from app.modules.retrieval.public import IndexChunk, Retrieval, index_representatives

from .errors import GateBusy, RecoveryFailed
from .gate import Gate, Operation, State
from .indexing import IndexingQueue


logger = logging.getLogger(__name__)


def verify_consistency(snapshots, entries):
    expected = {}
    for snapshot in snapshots:
        document = snapshot.document
        if document.index_status == 'INDEXING':
            raise RecoveryFailed('ORPHAN_INDEXING')
        if document.index_status != 'INDEXED':
            continue
        if not snapshot.chunks_valid:
            raise RecoveryFailed('INVALID_STORED_CHUNKS')
        indexed = index_representatives(
            IndexChunk(chunk.id, chunk.text, chunk.heading_path, document.tags) for chunk in snapshot.chunks)
        by_id = {chunk.id: chunk for chunk in snapshot.chunks}
        # Index sequences are dense after deduplication; SQL keeps the original order.
        for sequence, representative in enumerate(indexed):
            chunk = by_id[representative.chunk_id]
            expected[chunk.id] = (document.id, sequence, chunk.text, chunk.heading_path, document.tags)
    actual = {entry.chunk_id: (entry.document_id, entry.seq, entry.text, entry.heading_path, entry.tags)
              for entry in entries}
    if len(actual) != len(entries) or actual != expected:
        raise RecoveryFailed('INDEX_CONTENT_MISMATCH')


@dataclass(frozen=True)
class ReadyResult:
    state: State
    recovered: int


class Readiness:
    def __init__(self, knowledge: Knowledge, retrieval: Retrieval, queue: IndexingQueue, gate: Gate):
        self._knowledge, self._retrieval, self._queue, self._gate = knowledge,retrieval,queue,gate

    def ready(self) -> ReadyResult:
        admission = self._gate.try_acquire(Operation.RECOVERY)
        if admission.lease is None:
            raise GateBusy(admission.state)
        with admission.lease as lease:
            try:
                if self._knowledge.health()['status'] != 'UP' or self._retrieval.health()['status'] != 'UP':
                    raise RecoveryFailed('DEPENDENCIES_UNAVAILABLE')
                verify_consistency(self._knowledge.snapshots(), self._retrieval.inspect())
                lease.confirm_completion()
            except RecoveryFailed:
                raise
            except Exception as failure:
                raise RecoveryFailed(type(failure).__name__) from None
        # Open first: uploads whose jobs saw RECOVERING must be included in this scan.
        try:
            pending = self._knowledge.pending_ids()
            for document_id in pending:
                self._queue.submit(document_id)
        except Exception as failure:
            self._gate.require_recovery()
            raise RecoveryFailed(type(failure).__name__) from None
        return ReadyResult(self._gate.state, len(pending))


@dataclass(frozen=True)
class RebuildResult:
    documents: int
    chunks: int
    reused_chunks: int


def rebuild_index(knowledge: Knowledge, retrieval: Retrieval) -> RebuildResult:
    """Called by the maintenance CLI only after acquiring the process lock."""
    if knowledge.health()['status'] != 'UP':
        raise RecoveryFailed('DATABASE_UNAVAILABLE')
    health = retrieval.health()
    if any(health[part]['status'] != 'UP' for part in ('embedding','tokenizer')):
        raise RecoveryFailed('REBUILD_DEPENDENCIES_UNAVAILABLE')
    snapshots = knowledge.snapshots()
    retrieval.reset()
    total = reused = 0
    for snapshot in snapshots:
        document = snapshot.document
        try:
            drafts = retrieval.split(document.content, document.title)
            writes = tuple(ChunkWrite(seq,c.text,c.byte_start,c.byte_end,c.heading_path,c.token_count)
                           for seq,c in enumerate(drafts))
            chunks = knowledge.begin_rebuild(document.id, writes, expected_content=document.content)
            inputs = index_representatives(IndexChunk(c.id,c.text,c.heading_path,document.tags) for c in chunks)
            if retrieval.replace(document.id, inputs) != len(inputs):
                raise RecoveryFailed('REBUILT_COUNT_MISMATCH')
            knowledge.mark_indexed(document.id)
            total += len(chunks)
            reused += len({c.id for c in chunks} & {c.id for c in snapshot.chunks})
        except Exception as failure:
            diagnostic = f'REBUILD: {type(failure).__name__}'
            try:
                retrieval.delete_document(document.id)
            except Exception as cleanup_failure:
                diagnostic += f'; DELETE_INDEX: {type(cleanup_failure).__name__}'
            try:
                knowledge.mark_failed(document.id, diagnostic)
            except Exception as terminal_failure:
                logger.warning('rebuild_terminal_unconfirmed document_id=%s cause=%s',
                               document.id, type(terminal_failure).__name__)
            raise RecoveryFailed(diagnostic) from None
    verify_consistency(knowledge.snapshots(), retrieval.inspect())
    return RebuildResult(len(snapshots), total, reused)
