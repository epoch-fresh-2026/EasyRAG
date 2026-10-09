"""在线验收脚本的离线契约：真实 Qa + 替身模型/embedding，不发任何真实模型请求。"""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest
from tokenizers import Tokenizer
from tokenizers.models import WordLevel
from tokenizers.pre_tokenizers import Whitespace

from app.modules.qa.public import Qa
from app.modules.retrieval.public import Chunk
from scripts.eval_answers import (
    EvaluationChunk, Question, QuestionResult, check_faithfulness, cited_ranks, evaluate_question, render_report, split_sentences,
    summarize,
)
from scripts.eval_retrieval import IndexedChunk


class _ScriptedSession:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts = []

    @property
    def model_info(self):
        return {"provider": "openai", "model": "scripted"}

    def complete(self, prompt):
        self.prompts.append(("complete", prompt))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def stream(self, prompt):
        self.prompts.append(("stream", prompt))
        reply = self.replies.pop(0)
        for piece in re.findall(r".{1,4}", reply, flags=re.DOTALL):
            yield piece


class _FixedRetrieval:
    def __init__(self, hits):
        self.hits = hits

    def search(self, query, top_k=5, *, record_timing=None):
        if record_timing is not None:
            record_timing("embedding", 7.0)
            record_timing("vector", 1.0)
        return self.hits[:top_k]


class _Hit:
    def __init__(self, chunk_id, document_id, text, score):
        self.chunk_id, self.document_id, self.text, self.heading_path, self.score = chunk_id, document_id, text, "", score


def _chunk(chunk_id, document_id, source, text):
    return EvaluationChunk(chunk_id, document_id, IndexedChunk(source, 1, Chunk(text, "", 0, len(text.encode()), 1)))


CHUNKS = {
    1: _chunk(1, 1, "a.md", "ACID 是原子性、一致性、隔离性、持久性。"),
    2: _chunk(2, 2, "b.md", "Redis 事务不支持回滚。"),
    3: _chunk(3, 1, "a.md", "隔离级别包括读未提交等。"),
}
HITS = [_Hit(2, 2, CHUNKS[2].indexed.chunk.text, 0.7), _Hit(1, 1, CHUNKS[1].indexed.chunk.text, 0.6),
        _Hit(3, 1, CHUNKS[3].indexed.chunk.text, 0.5)]


@pytest.mark.parametrize("markdown,expected", [
    ("结论 [1]。", [1]), ("A [1, 3] B [2-4]", [1, 2, 3, 4]), ("[3，1] 与 [5–5]", [1, 3, 5]),
    ("没有引用", []), ("倒序 [4-2]", [4]),
])
def test_cited_ranks_expand_lists_and_ranges(markdown, expected):
    assert cited_ranks(markdown) == expected


def test_split_sentences_drops_code_markers_lead_ins_and_bold_headings_but_keeps_claims():
    markdown = ("## 结论\n- ACID 有四个特性 [1]。\n```python\nprint('x')\n```\n1. 隔离级别有四种！其中串行化最慢 [1]\n"
                "> 引用块。\n短\n它们各自的特点如下：\n**1. RDB（快照式持久化）**\n*   **特点**：\n    *   **文件小**：恢复快 [1]。")
    assert split_sentences(markdown) == ["ACID 有四个特性 [1]。", "隔离级别有四种！", "其中串行化最慢 [1]", "引用块。",
                                         "**文件小**：恢复快 [1]。"]


def test_model_unavailable_is_retried_with_cooldown_but_contract_failures_are_not(monkeypatch):
    from app.modules.answer_models.public import ModelUnavailable
    from scripts import eval_answers

    sleeps = []
    monkeypatch.setattr(eval_answers.time, "sleep", sleeps.append)
    session = _ScriptedSession(ModelUnavailable(), ModelUnavailable(), '{"verdict": "SUFFICIENT"}', "ACID [2]。")
    result = evaluate_question(Qa(), Question("Q1", "什么是 ACID？", "a.md"), _FixedRetrieval(HITS), session, 5, CHUNKS,
                               faithfulness=False, cooldown=2.0, max_attempts=3)
    assert (result.attempts, result.status, result.model_calls) == (3, "ANSWERED", 4)
    assert sleeps == [2.0, 4.0] and result.stage_ms.keys() >= {"judge", "generate"}

    session = _ScriptedSession('{"verdict": "SUFFICIENT"}', "没有引用。")
    result = evaluate_question(Qa(), Question("Q1", "什么是 ACID？", "a.md"), _FixedRetrieval(HITS), session, 5, CHUNKS,
                               faithfulness=False, cooldown=2.0, max_attempts=3)
    assert (result.attempts, result.failure) == (1, "generate:INVALID_CITATIONS") and sleeps == [2.0, 4.0]

    session = _ScriptedSession(ModelUnavailable(), ModelUnavailable())
    result = evaluate_question(Qa(), Question("Q1", "什么是 ACID？", "a.md"), _FixedRetrieval(HITS), session, 5, CHUNKS,
                               faithfulness=False, cooldown=1.0, max_attempts=2)
    assert (result.attempts, result.failure) == (2, "judge:ModelUnavailable") and sleeps == [2.0, 4.0, 1.0]


def test_refusal_records_one_call_and_no_first_text():
    session = _ScriptedSession('{"verdict": "NONE"}')
    result = evaluate_question(Qa(), Question("N1", "Redis GEO？", None), _FixedRetrieval(HITS), session, 5, CHUNKS,
                               faithfulness=True)
    assert (result.decision, result.status, result.failure) == ("NONE", "REFUSED", None)
    assert result.model_calls == 1 and result.first_text_ms is None and result.supported is None
    assert [candidate["source"] for candidate in result.candidates] == ["b.md", "a.md", "a.md"]
    assert result.stage_ms == {"embedding": 7.0, "vector": 1.0, "judge": pytest.approx(result.stage_ms["judge"])}


def test_wrong_source_is_flagged_from_cited_ranks_not_from_candidates():
    session = _ScriptedSession('{"verdict": "SUFFICIENT"}', "Redis 事务不回滚 [1]。")
    result = evaluate_question(Qa(), Question("Q1", "什么是 ACID？", "a.md"), _FixedRetrieval(HITS), session, 5, CHUNKS,
                               faithfulness=False)
    assert (result.decision, result.status) == ("SUFFICIENT", "ANSWERED")
    assert result.expected_rank == 2 and result.cited_ranks == [1] and result.cited_sources == ["b.md"]
    assert result.wrong_source is True and result.decision_expected is True
    assert result.first_text_ms is not None and result.model_calls == 2
    assert result.generate_prompt_chars == len(session.prompts[1][1])


def test_invalid_citations_are_a_failure_with_the_partial_answer_kept():
    session = _ScriptedSession('{"verdict": "PARTIAL"}', "没有任何引用编号的回答。")
    result = evaluate_question(Qa(), Question("P1", "部分", None), _FixedRetrieval(HITS), session, 5, CHUNKS,
                               faithfulness=True)
    assert result.failure == "generate:INVALID_CITATIONS" and result.status is None
    assert result.decision == "PARTIAL" and result.answer == "没有任何引用编号的回答。"
    assert result.supported is None and result.wrong_source is None


@pytest.mark.parametrize("answer,evidence,supported", [
    ("基于提供的资料，Redis 默认端口为 9999 [1]。", "Redis 默认端口为 6379。", False),
    ("以上回答严格基于片段，但 Redis 默认端口为 9999 [1]。", "Redis 默认端口为 6379。", False),
    ("Redis 默认端口为 9999，以上回答严格基于片段 [1]。", "Redis 默认端口为 6379。", False),
    ("覆盖边界说明：Redis 默认端口为 9999 [1]。", "Redis 默认端口为 6379。", False),
    ("无法确定超时时间，但 Redis 默认端口为 9999 [1]。", "Redis 默认端口为 6379。", False),
    ("资料未提及超时时间，Redis 默认端口为 9999 [1]。", "Redis 默认端口为 6379。", False),
    ("Redis 事务未包含回滚功能 [1]。", "Redis 事务不支持回滚。", True),
    ("覆盖边界说明：库中只有这些内容 [1]。", "Redis 默认端口为 6379。", False),
    ("Redis 默认端口为 6379 [1]。", "Redis 默认端口为 6379。", True),
])
def test_faithfulness_checks_facts_even_with_boundary_or_negative_wording(answer, evidence, supported):
    result = QuestionResult("Q1", "Q", "Redis？", "redis.md", answer=answer)
    session = _ScriptedSession(json.dumps({"supported": supported}))
    check_faithfulness(result, session, {1: evidence})
    assert len(session.prompts) == 1, 'a cited factual claim was silently excluded'
    assert answer in session.prompts[0][1] and evidence in session.prompts[0][1]
    assert (result.meta_sentences, result.support_calls, result.support_invalid) == (0, 1, 0)
    assert (result.supported, result.unsupported) == (int(supported), int(not supported))


@pytest.mark.parametrize("answer", [
    "以上回答严格基于片段 [1]。",
    "以上回答仅基于提供的资料 [1, 2]。",
    "覆盖边界说明：无法回答该问题 [1]。",
    "无法确定 [1]。",
])
def test_faithfulness_only_skips_recognized_complete_meta_sentences(answer):
    result = QuestionResult("P1", "P", "边界？", None, answer=answer)
    session = _ScriptedSession()
    check_faithfulness(result, session, {1: "片段一", 2: "片段二"})
    assert session.prompts == []
    assert (result.meta_sentences, result.support_calls, result.supported, result.unsupported) == (1, 0, 0, 0)
    assert summarize([result])["faithfulness"]["support_rate"] is None


def test_faithfulness_counts_supported_unsupported_invalid_and_uncited_sentences(monkeypatch):
    from scripts import eval_answers

    sleeps = []
    monkeypatch.setattr(eval_answers.time, "sleep", sleeps.append)
    answer = ("ACID 有四个特性 [2]。\n它们是原子性等 [2]。\n据说还有第五个特性 [2]。\n无引用的总结。\n"
              "覆盖边界说明：库中只有这些内容 [2]。\n以上回答严格基于片段 [2]。")
    session = _ScriptedSession('{"verdict": "SUFFICIENT"}', answer,
                               '{"supported": true}', 'not json', RuntimeError("model down"), '{"supported": false}',
                               '{"supported": false}')
    result = evaluate_question(Qa(), Question("Q2", "ACID？", "a.md"), _FixedRetrieval(HITS), session, 5, CHUNKS,
                               faithfulness=True, pause=0.5, cooldown=3.0, max_attempts=2)
    assert (result.supported, result.unsupported, result.support_invalid) == (1, 2, 1)
    assert result.unsupported_sentences == [
        {"sentence": "据说还有第五个特性 [2]。", "ranks": [2]},
        {"sentence": "覆盖边界说明：库中只有这些内容 [2]。", "ranks": [2]},
    ]
    # A claim about the entire library's contents is not a pure disclaimer.
    assert result.support_calls == 4 and result.meta_sentences == 1
    assert result.sentences == 6 and result.uncited_sentences == 1
    assert result.wrong_source is False
    # pause before the first check, pause between checks, and one cooldown for the failed attempt
    assert sleeps == [0.5, 0.5, 0.5, 3.0, 0.5]
    support_prompt = session.prompts[2][1]
    assert "ACID 有四个特性 [2]。" in support_prompt and CHUNKS[1].indexed.chunk.text in support_prompt
    assert CHUNKS[2].indexed.chunk.text not in support_prompt
    faith = summarize([result])["faithfulness"]
    assert faith["checked_sentences"] == 3 and faith["support_rate"] == pytest.approx(1 / 3)
    assert (faith["invalid"], faith["uncited_sentences"], faith["meta_sentences"]) == (1, 1, 1)


def test_relevant_subset_is_recorded_and_summarised(monkeypatch):
    session = _ScriptedSession('{"verdict": "SUFFICIENT", "relevant": [2]}', "ACID 有四个特性 [2]。")
    result = evaluate_question(Qa(), Question("Q1", "什么是 ACID？", "a.md"), _FixedRetrieval(HITS), session, 5, CHUNKS,
                               faithfulness=False)
    assert result.relevant_ranks == [2] and result.cited_ranks == [2] and result.wrong_source is False
    assert result.generate_prompt_chars == len(session.prompts[1][1]) and "Redis 事务不支持回滚" not in session.prompts[1][1]
    dropped = QuestionResult("Q2", "Q", "q", "b.md", decision="PARTIAL", status="PARTIAL", expected_rank=1,
                             relevant_ranks=[3], candidates=[{"rank": 1, "source": "b.md"}, {"rank": 2, "source": "b.md"},
                                                             {"rank": 3, "source": "c.md"}])
    kept = QuestionResult("Q3", "Q", "q", "b.md", decision="SUFFICIENT", status="ANSWERED", expected_rank=1,
                          relevant_ranks=[2], candidates=[{"rank": 1, "source": "b.md"}, {"rank": 2, "source": "b.md"}])
    summary = summarize([result, dropped, kept])
    # Q3 dropped rank 1 but kept another chunk of the expected document, so only Q2 lost its source
    assert summary["evidence"] == {"answers": 3, "candidates": 8, "relevant": 3, "subset_answers": 3,
                                   "expected_dropped_ids": ["Q2"]}


def test_summary_and_report_expose_rates_without_absolute_paths():
    answered = QuestionResult("Q1", "Q", "q", "a.md", decision="SUFFICIENT", status="ANSWERED", expected_rank=1,
                              cited_ranks=[1], cited_sources=["a.md"], wrong_source=False, first_text_ms=900.0,
                              total_ms=4000.0, stage_ms={"judge": 400.0, "generate": 3000.0}, model_calls=2,
                              generate_prompt_chars=800, sentences=3, uncited_sentences=1, supported=2,
                              unsupported=0, support_invalid=0, support_calls=2)
    wrong = QuestionResult("Q2", "Q", "q", "b.md", decision="PARTIAL", status="PARTIAL", expected_rank=None,
                           cited_ranks=[2], cited_sources=["c.md"], wrong_source=True, first_text_ms=1200.0,
                           total_ms=5000.0, model_calls=2, supported=1, unsupported=1, support_invalid=0, support_calls=2)
    refused = QuestionResult("N1", "N", "q", None, decision="NONE", status="REFUSED", total_ms=1500.0, model_calls=1)
    failed = QuestionResult("P1", "P", "q", None, decision="PARTIAL", failure="generate:INVALID_CITATIONS",
                            total_ms=6000.0, model_calls=2)
    summary = summarize([answered, wrong, refused, failed])
    assert summary["categories"]["Q"]["expected_decisions"] == 2 and summary["categories"]["N"]["expected_decisions"] == 1
    assert summary["categories"]["P"] == {"total": 1, "decisions": {"PARTIAL": 1},
                                          "statuses": {"generate:INVALID_CITATIONS": 1}, "expected_decisions": 1, "failures": 1}
    assert summary["sources"] == {"scored": 2, "expected_in_candidates": 1, "answered_with_citations": 2, "wrong_source": 1,
                                  "wrong_source_ids": ["Q2"], "missing_expected_ids": ["Q2"]}
    assert summary["faithfulness"]["support_rate"] == 0.75 and summary["faithfulness"]["unsupported_ids"] == ["Q2"]
    assert summary["latency_ms"]["first_text_median"] == 1050.0 and summary["latency_ms"]["refusal_total_median"] == 1500.0
    assert summary["calls"] == {"question_calls": 7, "support_calls": 4, "retried_questions": 0, "unavailable_after_retries": 0}
    assert summary["evidence"] == {"answers": 0, "candidates": 0, "relevant": 0, "subset_answers": 0, "expected_dropped_ids": []}
    report = render_report({
        "generated_at": "2026-09-21T00:00:00+08:00",
        "parameters": {"llm_provider": "openai", "llm_model": "scripted", "retrieval_strategy": "dense",
                       "embedding_model": "bge-m3", "embedding_dim": 3, "top_k": 5, "faithfulness": True, "only": None,
                       "pause": 1.0, "cooldown": 15.0, "max_attempts": 3,
                       "max_tokens": 512, "min_tokens": 64, "tokenizer_sha256": "deadbeef"},
        "document_count": 3, "chunk_count": 3, "index_seconds": 1.5, "corpus_sha256": "c0ffee",
        "golden_sha256": {"golden.md": "abc"}, "results": [json.loads(json.dumps(vars(result))) for result in
                                                            (answered, wrong, refused, failed)],
        "summary": summary, "versions": {"Python": "3.14"}, "code_sha256": {"scripts/eval_answers.py": "1234"},
        "command": ".\\.venv\\Scripts\\python.exe -m scripts.eval_answers --golden-set '../docs/eval/golden.md'",
    })
    assert "| N（库外） | 1 | NONE | 1/1（100.0%） |" in report
    assert "错源率（已作答且有引用的 Q/R 中，引用片段全部不属于标注出处）：1/2（50.0%）；错源题：Q2。" in report
    assert "引用支持率：75.0%" in report and "| P1 | P | PARTIAL | generate:INVALID_CITATIONS |" in report
    assert summary["faithfulness"]["metric_version"] == "cited_sentence_support_v2"
    assert "cited_sentence_support_v2" in report and "分母为取得有效判官结果的带引用句" in report
    assert "不能作为严格下界" in report
    assert re.search(r"[A-Za-z]:[/\\]", report) is None and "scripted" in report


@pytest.fixture
def evaluation_files(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "alpha.md").write_text("# Alpha\nalpha", encoding="utf-8")
    (corpus / "beta.md").write_text("# Beta\nbeta", encoding="utf-8")
    golden = tmp_path / "golden.md"
    golden.write_text("| Q1 | What is alpha? | alpha.md |\n| N1 | unknown | absent |\n", encoding="utf-8")
    tokenizer = Tokenizer(WordLevel({"[UNK]": 0, "alpha": 1, "beta": 2}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = Whitespace()
    tokenizer_path = tmp_path / "tokenizer.json"
    tokenizer.save(str(tokenizer_path))
    return corpus, golden, tokenizer_path


def test_cli_builds_an_isolated_index_and_writes_markdown_and_json(evaluation_files, tmp_path, monkeypatch, capsys):
    from scripts import eval_answers

    corpus, golden, tokenizer = evaluation_files
    original_client = httpx.Client
    embed_requests = []

    def respond(request):
        if request.method == "GET":
            return httpx.Response(200, json={"models": [{"name": "bge-m3:latest"}]})
        payload = json.loads(request.content)
        embed_requests.append(payload["input"])
        return httpx.Response(200, json={"embeddings": [
            [1.0, 0.0] if "alpha" in text.lower() else [0.0, 1.0] for text in payload["input"]
        ]})

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original_client(transport=httpx.MockTransport(respond), **kwargs))
    sessions = [_ScriptedSession('{"verdict": "SUFFICIENT"}', "alpha 是一个示例 [1]。", '{"supported": true}'),
                _ScriptedSession('{"verdict": "NONE"}')]

    class FakeModels:
        def __init__(self, config_path=None):
            self.config_path = config_path

        def prepare(self):
            pass

        def open_session(self):
            return sessions.pop(0)

    monkeypatch.setattr(eval_answers, "Models", FakeModels)
    created = []
    real_mkdtemp = eval_answers.tempfile.mkdtemp

    def tracking_mkdtemp(**kwargs):
        created.append(Path(real_mkdtemp(dir=str(tmp_path), **kwargs)))
        return str(created[-1])

    monkeypatch.setattr(eval_answers.tempfile, "mkdtemp", tracking_mkdtemp)
    output = tmp_path / "answers.md"
    status = eval_answers.main([
        "--corpus", str(corpus), "--golden-set", str(golden), "--tokenizer", str(tokenizer),
        "--ollama-url", "http://ollama", "--dimensions", "2", "--min-tokens", "0", "--faithfulness",
        "--pause", "0", "--cooldown", "0", "--output", str(output),
    ])

    assert status == 0
    report = output.read_text(encoding="utf-8")
    assert "| Q（直答） | 1 | SUFFICIENT / PARTIAL | 1/1（100.0%） |" in report
    assert "| N（库外） | 1 | NONE | 1/1（100.0%） |" in report
    assert "引用支持率：100.0%" in report and "错源题：无" in report
    assert "--tokenizer '<tokenizer.json>'" in report and str(tmp_path) not in report
    data = json.loads(output.with_suffix(".json").read_text(encoding="utf-8"))
    assert [result["question_id"] for result in data["results"]] == ["Q1", "N1"]
    assert data["results"][0]["cited_sources"] == ["alpha.md"] and data["results"][0]["wrong_source"] is False
    assert data["summary"]["faithfulness"]["metric_version"] == "cited_sentence_support_v2"
    assert data["summary"]["faithfulness"]["checked_sentences"] == 1
    assert data["summary"]["calls"] == {"question_calls": 3, "support_calls": 1, "retried_questions": 0,
                                        "unavailable_after_retries": 0}
    assert created and not created[0].exists()
    assert sum(len(batch) for batch in embed_requests) == 2 + 2
    assert "wrong source: 0/1" in capsys.readouterr().out


def test_cli_refuses_a_non_ascii_temporary_directory_before_opening_any_index(evaluation_files, tmp_path, monkeypatch):
    from scripts import eval_answers

    corpus, golden, tokenizer = evaluation_files
    unsafe = tmp_path / "临时"
    unsafe.mkdir()
    monkeypatch.setattr(eval_answers.tempfile, "mkdtemp", lambda **kwargs: str(unsafe))
    monkeypatch.setattr(eval_answers, "Retrieval", lambda settings: pytest.fail("index must not open"))
    with pytest.raises(SystemExit) as failure:
        eval_answers.main(["--corpus", str(corpus), "--golden-set", str(golden), "--tokenizer", str(tokenizer)])
    assert failure.value.code == 2 and not unsafe.exists()
