"""Recovery checks identities and ownership, not just matching vector counts."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.application.errors import GateBusy, RecoveryFailed
from app.application.gate import Gate, Operation, State
from app.application.indexing import IndexingQueue
from app.application.recovery import Readiness, rebuild_index
from app.modules.knowledge import public as knowledge
from app.modules.retrieval import public as retrieval


@pytest.fixture
def workflow():
    a,b,queue = Mock(spec=knowledge.Knowledge),Mock(spec=retrieval.Retrieval),Mock(spec=IndexingQueue)
    gate = Gate()
    doc = SimpleNamespace(id=1,content='hello',title='Note',tags=('tag',),index_status='INDEXED')
    chunk = knowledge.StoredChunk(101,1,0,'hello',0,5,'',1)
    snapshot = knowledge.DocumentSnapshot(doc,(chunk,),True)
    entry = retrieval.IndexEntry(101,1,0,'hello','',('tag',))
    a.health.return_value = {'status':'UP'}
    b.health.return_value = {'status':'UP','embedding':{'status':'UP'},'tokenizer':{'status':'UP'}}
    a.snapshots.return_value = (snapshot,)
    b.inspect.return_value = (entry,)
    a.pending_ids.return_value = (7,8)
    b.split.return_value = (retrieval.ChunkDraft('hello','',0,5,1),)
    a.begin_rebuild.return_value = (chunk,)
    b.replace.return_value = 1
    return SimpleNamespace(a=a,b=b,queue=queue,gate=gate,doc=doc,snapshot=snapshot,entry=entry,
                           readiness=Readiness(a,b,queue,gate))


def test_ready_verifies_under_exclusive_lease_then_opens_before_pending_scan(workflow):
    w=workflow
    def inspect():
        assert w.gate.state == State.RECOVERING
        assert w.gate.try_acquire(Operation.MUTATION).lease is None
        return (w.entry,)
    def pending():
        assert w.gate.state == State.READY
        return (7,8)
    w.b.inspect.side_effect=inspect
    w.a.pending_ids.side_effect=pending
    result=w.readiness.ready()
    assert result.state == State.READY and result.recovered == 2
    assert [call.args for call in w.queue.submit.call_args_list] == [(7,),(8,)]
    w.b.reset.assert_not_called()


@pytest.mark.parametrize('change', [
    {'chunk_id':102},{'document_id':2},{'seq':1},{'text':'wrong'},
    {'heading_path':'wrong'},{'tags':('wrong',)},
])
def test_equal_counts_cannot_hide_stale_or_wrong_index_metadata(workflow,change):
    w=workflow
    w.b.inspect.return_value=(replace(w.entry,**change),)
    with pytest.raises(RecoveryFailed):
        w.readiness.ready()
    assert w.gate.state == State.RECOVERY_REQUIRED
    w.queue.submit.assert_not_called()


@pytest.mark.parametrize('entries', [(),(retrieval.IndexEntry(101,1,0,'hello','',('tag',)),retrieval.IndexEntry(102,2,0,'orphan','',()))])
def test_missing_or_extra_vectors_require_offline_recovery(workflow,entries):
    w=workflow
    w.b.inspect.return_value=entries
    with pytest.raises(RecoveryFailed):
        w.readiness.ready()
    assert w.gate.state == State.RECOVERY_REQUIRED


@pytest.mark.parametrize('state,valid', [('INDEXING',True),('INDEXED',False)])
def test_orphan_state_or_invalid_stored_bytes_cannot_be_certified(workflow,state,valid):
    w=workflow
    w.doc.index_status=state
    w.a.snapshots.return_value=(replace(w.snapshot,chunks_valid=valid),)
    with pytest.raises(RecoveryFailed):
        w.readiness.ready()
    assert w.gate.state == State.RECOVERY_REQUIRED


def test_failed_documents_may_keep_sql_chunks_but_have_no_queryable_vectors(workflow):
    w=workflow
    w.doc.index_status='FAILED'
    w.b.inspect.return_value=()
    assert w.readiness.ready().state == State.READY


@pytest.fixture(params=[('dup','one','dup'), ('dup','dup','one')], ids=['trailing-duplicate', 'middle-duplicate'])
def repeated(workflow, request):
    w=workflow
    texts=request.param
    chunks=tuple(knowledge.StoredChunk(101+seq,1,seq,text,seq*3,seq*3+3,'',1) for seq,text in enumerate(texts))
    w.a.snapshots.return_value=(knowledge.DocumentSnapshot(w.doc,chunks,True),)
    w.b.split.return_value=tuple(retrieval.ChunkDraft(text,'',seq*3,seq*3+3,1) for seq,text in enumerate(texts))
    w.a.begin_rebuild.return_value=chunks
    w.entries=tuple(retrieval.IndexEntry(c.id,1,c.seq,c.text,'',('tag',)) for c in chunks)
    w.inputs=tuple(retrieval.IndexChunk(c.id,c.text,'',('tag',)) for i,c in enumerate(chunks) if c.text not in texts[:i])
    w.expected=tuple(retrieval.IndexEntry(c.chunk_id,1,seq,c.text,'',('tag',)) for seq,c in enumerate(w.inputs))
    return w


def test_ready_expects_only_the_first_of_identical_chunks_in_the_index(repeated):
    w=repeated
    w.b.inspect.return_value=w.expected
    assert w.readiness.ready().state == State.READY


def test_representative_sequence_must_be_compact_even_when_sql_has_gaps(repeated):
    w=repeated
    w.b.inspect.return_value=(w.expected[0],replace(w.expected[1],seq=2))
    with pytest.raises(RecoveryFailed):
        w.readiness.ready()
    assert w.gate.state == State.RECOVERY_REQUIRED
    w.queue.submit.assert_not_called()


@pytest.mark.parametrize('kept', [(0,1,2),(1,2),(0,)])
def test_stale_full_index_or_wrong_representative_requires_offline_recovery(repeated,kept):
    w=repeated
    w.b.inspect.return_value=tuple(w.entries[i] for i in kept)
    with pytest.raises(RecoveryFailed):
        w.readiness.ready()
    assert w.gate.state == State.RECOVERY_REQUIRED


def test_offline_rebuild_stores_every_chunk_but_embeds_identical_inputs_once(repeated):
    w=repeated
    w.b.replace.return_value=2
    w.b.inspect.return_value=w.expected
    result=rebuild_index(w.a,w.b)
    assert result.documents == 1 and result.chunks == 3
    assert len(w.a.begin_rebuild.call_args.args[1]) == 3
    w.b.replace.assert_called_once_with(1,w.inputs)
    w.b.replace.return_value=3
    with pytest.raises(RecoveryFailed):
        rebuild_index(w.a,w.b)


def test_dependency_failure_does_not_reset_or_open_anything(workflow):
    w=workflow
    w.b.health.return_value={'status':'DOWN'}
    with pytest.raises(RecoveryFailed):
        w.readiness.ready()
    assert w.gate.state == State.RECOVERY_REQUIRED
    w.b.reset.assert_not_called()
    w.a.pending_ids.assert_not_called()


def test_recovery_never_overlaps_a_live_query(workflow):
    w=workflow
    w.gate.try_acquire(Operation.RECOVERY).lease.confirm_completion()
    with w.gate.try_acquire(Operation.QUERY).lease:
        with pytest.raises(GateBusy):
            w.readiness.ready()
    w.a.health.assert_not_called()


def test_scan_failure_marks_recovery_even_when_new_worker_is_already_active(workflow):
    w=workflow
    workers=[]
    def pending():
        workers.append(w.gate.try_acquire(Operation.MUTATION).lease)
        raise knowledge.DatabaseUnavailable('scan failed')
    w.a.pending_ids.side_effect=pending
    with pytest.raises(RecoveryFailed):
        w.readiness.ready()
    workers[0].confirm_completion()
    assert w.gate.state == State.RECOVERY_REQUIRED


def test_offline_rebuild_repairs_all_documents_and_preserves_proven_ids(workflow):
    w=workflow
    result=rebuild_index(w.a,w.b)
    assert result.documents == 1 and result.chunks == 1 and result.reused_chunks == 1
    w.b.reset.assert_called_once_with()
    w.a.begin_rebuild.assert_called_once_with(1,(knowledge.ChunkWrite(0,'hello',0,5,'',1),),expected_content='hello')
    w.b.replace.assert_called_once_with(1,(retrieval.IndexChunk(101,'hello','',('tag',)),))
    w.a.mark_indexed.assert_called_once_with(1)


def test_offline_rebuild_can_reset_metadata_mismatch_but_requires_embedding(workflow):
    w=workflow
    w.b.health.return_value={'status':'DOWN','chroma':{'status':'DOWN'},'embedding':{'status':'UP'},'tokenizer':{'status':'UP'}}
    assert rebuild_index(w.a,w.b).documents == 1
    w.b.reset.reset_mock()
    w.b.health.return_value['embedding']['status']='DOWN'
    with pytest.raises(RecoveryFailed):
        rebuild_index(w.a,w.b)
    w.b.reset.assert_not_called()


def test_failed_rebuild_cleans_and_records_failure_without_reporting_success(workflow):
    w=workflow
    w.b.replace.side_effect=retrieval.RetrievalUnavailable('embedding','TIMEOUT')
    with pytest.raises(RecoveryFailed):
        rebuild_index(w.a,w.b)
    w.b.delete_document.assert_called_once_with(1)
    w.a.mark_failed.assert_called_once()
    w.a.mark_indexed.assert_not_called()
