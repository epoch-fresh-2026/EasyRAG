"""Offline rebuild recovery over isolated MySQL, real Chroma and local HTTP embeddings."""

import json
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from types import SimpleNamespace

import pytest
from tokenizers import Tokenizer
from tokenizers.models import WordLevel
from tokenizers.pre_tokenizers import WhitespaceSplit
from tokenizers.processors import TemplateProcessing

from app.application.errors import RecoveryFailed
from app.application.gate import Gate, Operation, State
from app.application.indexing import Indexer, IndexingQueue
from app.application.recovery import Readiness, rebuild_index, verify_consistency
from app.modules.knowledge import public as knowledge
from app.modules.retrieval import public as retrieval
from tests.mysql_knowledge_integration import sql
from tests.mysql_support import mysql_sandbox


@pytest.fixture
def embedding_server(monkeypatch):
    monkeypatch.setenv('NO_PROXY', '127.0.0.1,localhost')
    state = SimpleNamespace(posts=[], gets=[])

    class Handler(BaseHTTPRequestHandler):
        timeout = 3

        def log_message(self, *_):
            pass

        def respond(self, payload):
            body = json.dumps(payload).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            state.gets.append(self.path)
            if self.path != '/api/tags':
                self.send_error(404)
                return
            self.respond({'models': [{'name': 'rebuild-test:latest'}]})

        def do_POST(self):
            if self.path != '/api/embed':
                self.send_error(404)
                return
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state.posts.append(payload)
            vectors = []
            for text in payload['input']:
                digest = sha256(text.encode('utf-8')).digest()
                vectors.append([1.0, digest[0] / 255, digest[1] / 255])
            self.respond({'embeddings': vectors})

    server = HTTPServer(('127.0.0.1', 0), Handler)
    state.url = f'http://127.0.0.1:{server.server_port}'
    worker = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.02},
                    name=f'rebuild-embedding-{server.server_port}')
    worker.start()
    try:
        yield state
    finally:
        try:
            server.shutdown()
            worker.join(timeout=5)
        finally:
            server.server_close()
        assert not worker.is_alive(), 'local embedding server did not stop'


@pytest.fixture
def services(tmp_path, embedding_server):
    with ExitStack() as cleanup:
        sandbox = cleanup.enter_context(mysql_sandbox())
        directory = Path(cleanup.enter_context(TemporaryDirectory(prefix='rebuild-', dir=tmp_path)))
        tokenizer = Tokenizer(WordLevel({'[UNK]': 0, '[CLS]': 1, '[SEP]': 2}, unk_token='[UNK]'))
        tokenizer.pre_tokenizer = WhitespaceSplit()
        tokenizer.post_processor = TemplateProcessing(
            single='[CLS] $A [SEP]', special_tokens=[('[CLS]', 1), ('[SEP]', 2)],
        )
        tokenizer_path = directory / 'tokenizer.json'
        tokenizer.save(str(tokenizer_path))
        settings = retrieval.RetrievalSettings(
            _env_file=None, chroma_dir=directory / 'chroma', chunk_tokenizer_path=tokenizer_path,
            embedding_provider='ollama', embedding_model='rebuild-test', embedding_dim=3,
            embedding_base_url=embedding_server.url, embedding_api_key=None,
            embedding_timeout_seconds=3, embed_batch_size=2, chunk_max_tokens=64, chunk_min_tokens=0,
        )
        a = knowledge.Knowledge(sandbox.settings())
        cleanup.callback(a.close)
        a.initialize_database()
        b = retrieval.Retrieval(settings)
        cleanup.callback(b.close)
        yield SimpleNamespace(a=a, b=b, sandbox=sandbox, settings=settings,
                              cleanup=cleanup, provider=embedding_server)
    assert not directory.exists(), 'temporary tokenizer and Chroma files were not removed'


def seed_documents(services):
    body = ' '.join(f'word{number}' for number in range(36))
    sources = (
        ('first.md', f'---\ntitle: First\ntags: [guide, 中文]\n---\n# First\n{body}\n\n中文 🙂\n'),
        ('second.md', f'# Second\r\n{body}\r\n\r\n下一段 🙂\r\n'),
        ('short.md', 'short note.'),
    )
    documents = tuple(services.a.create(name, content.encode('utf-8')) for name, content in sources)
    for document in documents:
        sql(services.sandbox, "UPDATE document SET updated_at='2000-01-02 03:04:05' WHERE id=%s",
            (document.id,))
    return tuple(services.a.get(document.id) for document in documents)


def source_truth(service):
    fields = ('id', 'source_type', 'source_uri', 'title', 'content', 'content_hash', 'tags',
              'created_at', 'updated_at', 'deleted_at')
    return tuple(tuple(getattr(snapshot.document, field) for field in fields)
                 for snapshot in service.snapshots())


def chunk_ids(service):
    return {snapshot.document.id: tuple(chunk.id for chunk in snapshot.chunks)
            for snapshot in service.snapshots()}


@contextmanager
def readiness_for(a, b):
    gate = Gate()
    queue = IndexingQueue(Indexer(a, b, gate))
    try:
        yield Readiness(a, b, queue, gate), gate
    finally:
        queue.close()


@contextmanager
def recovery_failure(reason):
    with pytest.raises(RecoveryFailed) as captured:
        yield
    assert captured.value.reason == reason


def assert_complete(services, result, original_source, expected_reused, *, index=None):
    index = services.b if index is None else index
    snapshots, entries = services.a.snapshots(), index.inspect()
    assert result.documents == len(snapshots) == 3
    assert result.chunks == len(entries) == sum(len(snapshot.chunks) for snapshot in snapshots)
    assert result.reused_chunks == expected_reused
    assert source_truth(services.a) == original_source
    assert all(snapshot.document.index_status == 'INDEXED' and snapshot.document.index_error is None
               and snapshot.chunks_valid for snapshot in snapshots)
    verify_consistency(snapshots, entries)
    for snapshot in snapshots:
        body = snapshot.document.content.encode('utf-8')
        assert ''.join(chunk.text for chunk in snapshot.chunks) == snapshot.document.content
        for chunk in snapshot.chunks:
            assert body[chunk.byte_start:chunk.byte_end].decode('utf-8') == chunk.text
    assert services.provider.posts and services.provider.gets
    assert all(post['model'] == 'rebuild-test' and post['truncate'] is False
               for post in services.provider.posts)


def test_interrupted_multi_document_rebuild_reopens_and_reuses_matching_ids(services, monkeypatch):
    a, b = services.a, services.b
    documents = seed_documents(services)
    original_source = source_truth(a)
    initial = rebuild_index(a, b)
    original_ids = chunk_ids(a)
    written = []
    real_replace = b.replace

    def interrupt_after_write(document_id, chunks):
        count = real_replace(document_id, chunks)
        written.append(document_id)
        if document_id == documents[1].id:
            raise KeyboardInterrupt('injected after committed vector writes')
        return count

    with monkeypatch.context() as patch:
        patch.setattr(b, 'replace', interrupt_after_write)
        with pytest.raises(KeyboardInterrupt):
            rebuild_index(a, b)
    assert written == [documents[0].id, documents[1].id]
    assert [a.get(document.id).index_status for document in documents] == ['INDEXED', 'INDEXING', 'INDEXED']
    assert {entry.document_id for entry in b.inspect()} == set(written)
    assert source_truth(a) == original_source and chunk_ids(a) == original_ids

    # Close and reopen both stores to prove recovery reads committed state, not cached objects.
    a.close()
    b.close()
    a = knowledge.Knowledge(services.sandbox.settings())
    services.cleanup.callback(a.close)
    b = retrieval.Retrieval(services.settings)
    services.cleanup.callback(b.close)
    services.a, services.b = a, b
    with readiness_for(a, b) as (readiness, gate):
        with recovery_failure('ORPHAN_INDEXING'):
            verify_consistency(a.snapshots(), b.inspect())
        with recovery_failure('ORPHAN_INDEXING'):
            readiness.ready()
        assert gate.state == State.RECOVERY_REQUIRED
        assert gate.try_acquire(Operation.QUERY).lease is None
        result = rebuild_index(a, b)
        assert_complete(services, result, original_source, initial.chunks)
        assert chunk_ids(a) == original_ids
        ready = readiness.ready()
        assert ready.state == State.READY and ready.recovered == 0


def test_failed_write_and_cleanup_leave_stale_vectors_until_successful_rebuild(services, monkeypatch):
    a, b = services.a, services.b
    documents = seed_documents(services)
    original_source = source_truth(a)
    initial = rebuild_index(a, b)
    original_ids = chunk_ids(a)
    real_replace = b.replace
    written, cleanup_attempts = [], []

    def fail_after_write(document_id, chunks):
        count = real_replace(document_id, chunks)
        written.append(document_id)
        if document_id == documents[-1].id:
            raise retrieval.RetrievalUnavailable('index', 'INJECTED_AFTER_WRITE')
        return count

    def fail_cleanup(document_id):
        cleanup_attempts.append(document_id)
        raise retrieval.RetrievalUnavailable('index', 'INJECTED_CLEANUP')

    with monkeypatch.context() as patch:
        patch.setattr(b, 'replace', fail_after_write)
        patch.setattr(b, 'delete_document', fail_cleanup)
        with recovery_failure('REBUILD: RetrievalUnavailable; DELETE_INDEX: RetrievalUnavailable'):
            rebuild_index(a, b)
    assert written == [document.id for document in documents]
    assert cleanup_attempts == [documents[-1].id]
    failed = a.get(documents[-1].id)
    assert failed.index_status == 'FAILED' and 'DELETE_INDEX' in failed.index_error
    assert all(a.get(document.id).index_status == 'INDEXED' for document in documents[:-1])
    assert {entry.chunk_id for entry in b.inspect() if entry.document_id == failed.id} == set(original_ids[failed.id])
    assert len(b.inspect()) == initial.chunks
    assert source_truth(a) == original_source and chunk_ids(a) == original_ids

    with readiness_for(a, b) as (readiness, gate):
        with recovery_failure('INDEX_CONTENT_MISMATCH'):
            verify_consistency(a.snapshots(), b.inspect())
        with recovery_failure('INDEX_CONTENT_MISMATCH'):
            readiness.ready()
        assert gate.state == State.RECOVERY_REQUIRED
        assert gate.try_acquire(Operation.QUERY).lease is None
        result = rebuild_index(a, b)
        assert_complete(services, result, original_source, initial.chunks)
        assert chunk_ids(a) == original_ids
        ready = readiness.ready()
        assert ready.state == State.READY and ready.recovered == 0


def test_changed_chunk_budget_rechunks_long_documents_without_stale_vectors(services):
    a, b = services.a, services.b
    documents = seed_documents(services)
    original_source = source_truth(a)
    initial = rebuild_index(a, b)
    original_ids = chunk_ids(a)
    b.close()
    changed_settings = services.settings.model_copy(update={'chunk_max_tokens': 12})
    changed = retrieval.Retrieval(changed_settings)
    services.cleanup.callback(changed.close)
    result = rebuild_index(a, changed)
    current_ids = chunk_ids(a)
    assert result.chunks > initial.chunks
    for document in documents[:-1]:
        assert len(current_ids[document.id]) > len(original_ids[document.id])
        assert set(current_ids[document.id]).isdisjoint(original_ids[document.id])
    assert current_ids[documents[-1].id] == original_ids[documents[-1].id]
    obsolete = {identifier for document in documents[:-1] for identifier in original_ids[document.id]}
    assert obsolete.isdisjoint(entry.chunk_id for entry in changed.inspect())
    assert a.sources(tuple(obsolete)) == ()
    assert_complete(services, result, original_source, len(original_ids[documents[-1].id]), index=changed)
    with readiness_for(a, changed) as (readiness, _):
        ready = readiness.ready()
        assert ready.state == State.READY and ready.recovered == 0


def test_final_rebuild_audit_rejects_equal_counts_with_wrong_metadata(services, monkeypatch):
    a, b = services.a, services.b
    documents = seed_documents(services)
    original_source = source_truth(a)
    initial = rebuild_index(a, b)
    original_ids = chunk_ids(a)
    real_replace = b.replace
    written = []

    def replace_with_stale_tags(document_id, chunks):
        if document_id == documents[-1].id:
            chunks = tuple(replace(chunk, tags=('stale',)) for chunk in chunks)
        count = real_replace(document_id, chunks)
        written.append(document_id)
        return count

    with monkeypatch.context() as patch:
        patch.setattr(b, 'replace', replace_with_stale_tags)
        with recovery_failure('INDEX_CONTENT_MISMATCH'):
            rebuild_index(a, b)
    assert written == [document.id for document in documents]
    assert all(a.get(document.id).index_status == 'INDEXED' for document in documents)
    assert len(b.inspect()) == initial.chunks
    assert source_truth(a) == original_source and chunk_ids(a) == original_ids
    with readiness_for(a, b) as (readiness, gate):
        with recovery_failure('INDEX_CONTENT_MISMATCH'):
            readiness.ready()
        assert gate.state == State.RECOVERY_REQUIRED
        result = rebuild_index(a, b)
        assert_complete(services, result, original_source, initial.chunks)
        assert chunk_ids(a) == original_ids
        ready = readiness.ready()
        assert ready.state == State.READY and ready.recovered == 0


@pytest.mark.parametrize('body', [
    'A plain note without headings.\n',
    '# ' + 'A' * 600 + '\nBody.\n',
    'start\n' + ' ' * 70000 + '\nend',
    'start' + ' ' * 70000,
    ' ' * 70000 + 'end',
    '开始\n' + '\u3000' * 23000 + '\n结束',
], ids=['plain', 'long-heading', 'middle-space', 'trailing-space', 'leading-space', 'unicode-space'])
def test_adversarial_chunk_drafts_persist_and_rebuild_without_losing_source(services, body):
    a, b = services.a, services.b
    with readiness_for(a, b) as (readiness, gate):
        assert readiness.ready().state == State.READY
        document = a.create('storage-contract.md', body.encode('utf-8'))
        result = Indexer(a, b, gate).run(document.id)
        assert result.outcome == 'INDEXED', result
        assert a.get(document.id).content == body
        snapshots = a.snapshots()
        assert snapshots[0].chunks_valid
        verify_consistency(snapshots, b.inspect())
        original_ids = chunk_ids(a)
        original_source = source_truth(a)

        rebuilt = rebuild_index(a, b)
        assert rebuilt.reused_chunks == rebuilt.chunks == len(original_ids[document.id])
        assert chunk_ids(a) == original_ids
        assert source_truth(a) == original_source
        verify_consistency(a.snapshots(), b.inspect())
        assert readiness.ready().state == State.READY


@pytest.mark.parametrize('paragraphs', [('dup', 'dup', 'one'), ('dup', 'one', 'dup', 'two')])
def test_middle_duplicates_survive_reopen_and_rebuild_with_compact_index_order(services, paragraphs):
    a, b = services.a, services.b
    body = ''.join((text + ' ') * 40 + '\n\n' for text in paragraphs)
    with readiness_for(a, b) as (readiness, gate):
        assert readiness.ready().state == State.READY
        document = a.create('middle-duplicates.md', body.encode('utf-8'))
        assert Indexer(a, b, gate).run(document.id).outcome == 'INDEXED'
        rows = a.snapshots()[0].chunks
        assert len(rows) == len(paragraphs)
        assert [row.seq for row in rows] == list(range(len(paragraphs)))
        kept = [row for index, row in enumerate(rows) if paragraphs[index] not in paragraphs[:index]]
        expected = {row.id: sequence for sequence, row in enumerate(kept)}
        assert {entry.chunk_id: entry.seq for entry in b.inspect()} == expected
        original_ids, original_source = chunk_ids(a), source_truth(a)
        assert readiness.ready().state == State.READY

    a.close()
    b.close()
    a = knowledge.Knowledge(services.sandbox.settings())
    b = retrieval.Retrieval(services.settings)
    services.cleanup.callback(a.close)
    services.cleanup.callback(b.close)
    with readiness_for(a, b) as (readiness, _):
        assert readiness.ready().state == State.READY
        rebuilt = rebuild_index(a, b)
        assert rebuilt.chunks == rebuilt.reused_chunks == len(rows)
        assert chunk_ids(a) == original_ids and source_truth(a) == original_source
        assert {entry.chunk_id: entry.seq for entry in b.inspect()} == expected
        assert readiness.ready().state == State.READY


def test_repeated_paragraphs_keep_every_row_but_one_vector_and_stale_full_index_is_repaired(services):
    a, b = services.a, services.b
    body = ('dup ' * 40 + '\n\n') * 64
    with readiness_for(a, b) as (readiness, gate):
        assert readiness.ready().state == State.READY
        document = a.create('repeated.md', body.encode('utf-8'))
        assert Indexer(a, b, gate).run(document.id).outcome == 'INDEXED'
        rows = a.snapshots()[0].chunks
        assert a.get(document.id).chunk_count == len(rows) == 64
        assert len({chunk.text for chunk in rows}) == 1
        entries = b.inspect()
        assert [entry.chunk_id for entry in entries] == [rows[0].id]
        verify_consistency(a.snapshots(), entries)
        assert readiness.ready().state == State.READY
        hits = b.search('dup', top_k=5)
        assert [hit.chunk_id for hit in hits] == [rows[0].id]

        b.replace(document.id, tuple(retrieval.IndexChunk(c.id, c.text, c.heading_path, ()) for c in rows))
        assert len(b.inspect()) == 64
        with pytest.raises(RecoveryFailed):
            readiness.ready()
        assert gate.state == State.RECOVERY_REQUIRED

        rebuilt = rebuild_index(a, b)
        assert rebuilt.chunks == rebuilt.reused_chunks == 64
        assert [entry.chunk_id for entry in b.inspect()] == [rows[0].id]
        assert readiness.ready().state == State.READY
