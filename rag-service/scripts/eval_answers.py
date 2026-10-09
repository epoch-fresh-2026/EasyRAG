"""子 Issue D 的在线问答验收：样例语料建隔离索引，经真实判定/生成模型跑黄金集。

不接业务 MySQL，不打开业务 Chroma；付费模型调用只发生在本脚本内并逐题记录。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import shutil
import statistics
import sys
import tempfile
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
from importlib.metadata import version
from pathlib import Path
from time import perf_counter

from app.modules.answer_models.public import Models
from app.modules.qa.public import Evidence, Qa, QaError
from app.modules.retrieval.public import (
    IndexChunk, Retrieval, RetrievalSettings, index_representatives, splitter_fingerprint,
)
from scripts.eval_retrieval import (
    PROJECT_ROOT, SERVICE_DIR, TOKENIZER_REVISION, TOKENIZER_SHA256, TOKENIZER_URL,
    IndexedChunk, Question, _cell, _quote, load_tokenizer, parse_questions, prepare_corpus,
)


DEFAULT_GOLDEN_SETS = (PROJECT_ROOT / "docs/eval/golden-set-v1.md", PROJECT_ROOT / "docs/eval/golden-set-v2-heldout.md")
EXPECTED_DECISIONS = {"Q": ("SUFFICIENT", "PARTIAL"), "R": ("SUFFICIENT", "PARTIAL"), "N": ("NONE",), "P": ("PARTIAL",)}
CATEGORY_LABELS = {"Q": "直答", "R": "需改写", "N": "库外", "P": "部分覆盖"}
MIN_SENTENCE_CHARS = 4
FAITHFULNESS_METRIC_VERSION = "cited_sentence_support_v2"
RETRY_ACCOUNTING_VERSION = "retry_accounting_v2"
_CITATION = re.compile(r"\[(\d+(?:\s*[,，\-–]\s*\d+)*)\]")
_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)
# Only complete, narrow templates are exempt; unfamiliar or mixed wording must be checked.
_META_SENTENCE = re.compile(
    r"(?:覆盖边界(?:说明)?[：:]\s*)?"
    r"(?:(?:以上|上述|本次)回答(?:仅|严格)?基于(?:提供的|已有的?|上述|以上)?(?:知识库)?(?:资料|片段)"
    r"|无法回答(?:该问题|这个问题|此问题)?|无法确定)"
)


def is_meta_sentence(sentence: str) -> bool:
    text = _CITATION.sub("", sentence).strip().rstrip("。！？!?.").strip()
    return _META_SENTENCE.fullmatch(text) is not None


@dataclass(frozen=True)
class EvaluationChunk:
    chunk_id: int
    document_id: int
    indexed: IndexedChunk


@dataclass
class QuestionResult:
    question_id: str
    category: str
    text: str
    expected_source: str | None
    decision: str | None = None
    status: str | None = None
    failure: str | None = None
    answer: str = ""
    candidates: list[dict] = field(default_factory=list)
    relevant_ranks: list[int] = field(default_factory=list)
    expected_rank: int | None = None
    cited_ranks: list[int] = field(default_factory=list)
    cited_sources: list[str] = field(default_factory=list)
    wrong_source: bool | None = None
    first_text_ms: float | None = None
    total_ms: float = 0.0
    last_attempt_ms: float = 0.0
    execution_ms: float = 0.0
    backoff_ms: float = 0.0
    attempt_history: list[dict] = field(default_factory=list)
    stage_ms: dict[str, float] = field(default_factory=dict)
    model_calls: int = 0
    attempts: int = 1
    generate_prompt_chars: int | None = None
    sentences: int = 0
    uncited_sentences: int = 0
    supported: int | None = None
    unsupported: int | None = None
    support_invalid: int | None = None
    support_calls: int = 0
    support_sentences: int = 0
    meta_sentences: int = 0
    unsupported_sentences: list[dict] = field(default_factory=list)

    @property
    def decision_expected(self) -> bool | None:
        if self.decision is None:
            return None
        return self.decision in EXPECTED_DECISIONS[self.category]


class _RecordingSession:
    """Count model calls and prompt sizes without changing what the model sees."""

    def __init__(self, session) -> None:
        self._session = session
        self.calls: list[tuple[str, int]] = []

    @property
    def model_info(self) -> dict[str, str]:
        return self._session.model_info

    def complete(self, prompt: str) -> str:
        self.calls.append(("complete", len(prompt)))
        return self._session.complete(prompt)

    def stream(self, prompt: str):
        self.calls.append(("stream", len(prompt)))
        return self._session.stream(prompt)


class _EvidenceSearch:
    """Adapt B's hits to C's evidence exactly as the application layer does."""

    def __init__(self, retrieval: Retrieval, record_timing) -> None:
        self._retrieval = retrieval
        self._record_timing = record_timing

    def search(self, query: str, top_k: int = 5) -> tuple[Evidence, ...]:
        return tuple(Evidence(hit.chunk_id, hit.document_id, hit.text, hit.heading_path, hit.score)
                     for hit in self._retrieval.search(query, top_k=top_k, record_timing=self._record_timing))


def cited_ranks(markdown: str) -> list[int]:
    ranks: set[int] = set()
    for match in _CITATION.finditer(markdown):
        for part in re.split(r"[,，]", re.sub(r"\s+", "", match.group(1))):
            bounds = [int(bound) for bound in re.split(r"[-–]", part)]
            if len(bounds) == 2 and bounds[0] <= bounds[1]:
                ranks.update(range(bounds[0], bounds[1] + 1))
            else:
                ranks.add(bounds[0])
    return sorted(ranks)


def split_sentences(markdown: str) -> list[str]:
    """Claims are checked per sentence; code, list/heading markers, bold-only headings and lead-ins are not claims."""
    text = re.sub(r"```.*?```", " ", markdown, flags=re.DOTALL)
    text = re.sub(r"^[ \t]*(?:[-*+]|\d+[.)]|#{1,6}|>)[ \t]+", "", text, flags=re.MULTILINE)
    pieces = [piece.strip() for piece in re.split(r"(?<=[。！？!?])|\n+", text) if piece]
    return [piece for piece in pieces
            if len(piece) >= MIN_SENTENCE_CHARS and not piece.endswith(("：", ":"))
            and not re.fullmatch(r"\*\*[^*]+\*\*[：:]?", piece)]


def support_prompt(sentence: str, cited: list[tuple[int, str]]) -> str:
    numbered = "\n\n".join(f"[{rank}] {text}" for rank, text in cited)
    return (
        "你是引用核对员。判断下面这句话是否完全由所引片段支持：句子里的每个事实、数字和限定条件"
        "都要能在片段中找到依据；片段之外的补充、推断或夸大都算不支持。\n\n"
        f"句子：{sentence}\n\n所引片段：\n{numbered}\n\n"
        '只输出 JSON：{"supported": true} 或 {"supported": false}，不要输出其他内容。'
    )


def parse_support(reply: str) -> bool | None:
    match = _JSON_OBJECT.search(reply)
    if match is None:
        return None
    try:
        supported = json.loads(match.group(0))["supported"]
    except (ValueError, KeyError, TypeError):
        return None
    return supported if isinstance(supported, bool) else None


def _complete_with_retries(session, prompt: str, cooldown: float, max_attempts: int) -> str | None:
    """Third-party endpoints rate-limit bursts; F reports every failure as ModelUnavailable, so cool down and retry."""
    for attempt in range(1, max_attempts + 1):
        try:
            return session.complete(prompt)
        except Exception:  # noqa: BLE001 - keep the results gathered so far, record this sentence as unchecked
            if attempt == max_attempts:
                return None
            time.sleep(cooldown * attempt)
    return None


def check_faithfulness(result: QuestionResult, session, rank_texts: dict[int, str], *,
                       pause: float = 0.0, cooldown: float = 0.0, max_attempts: int = 1) -> None:
    result.supported = result.unsupported = result.support_invalid = 0
    recording = _RecordingSession(session)
    for sentence in split_sentences(result.answer):
        ranks = [rank for rank in cited_ranks(sentence) if rank in rank_texts]
        if not ranks:
            continue
        if is_meta_sentence(sentence):
            result.meta_sentences += 1
            continue
        if result.support_calls and pause:
            time.sleep(pause)
        result.support_sentences += 1
        calls_before = len(recording.calls)
        reply = _complete_with_retries(recording, support_prompt(sentence, [(rank, rank_texts[rank]) for rank in ranks]),
                                       cooldown, max_attempts)
        result.support_calls += len(recording.calls) - calls_before
        verdict = None if reply is None else parse_support(reply)
        if verdict is None:
            result.support_invalid += 1
        elif verdict:
            result.supported += 1
        else:
            result.unsupported += 1
            result.unsupported_sentences.append({"sentence": sentence, "ranks": ranks})


def evaluate_question(
    qa: Qa, question: Question, retrieval: Retrieval, session, top_k: int,
    chunks: dict[int, EvaluationChunk], *, faithfulness: bool,
    pause: float = 0.0, cooldown: float = 0.0, max_attempts: int = 1,
) -> QuestionResult:
    result = QuestionResult(question.question_id, question.category, question.text, question.expected_source)

    def record_timing(stage: str, milliseconds: float) -> None:
        result.stage_ms[stage] = result.stage_ms.get(stage, 0.0) + milliseconds

    search = _EvidenceSearch(retrieval, record_timing)
    recording = _RecordingSession(session)
    parts: list[str] = []
    trace = ()
    draft = None
    question_started = perf_counter()
    for attempt in range(1, max_attempts + 1):
        result.attempts = attempt
        result.stage_ms.clear()
        result.first_text_ms = None
        result.failure = None
        parts.clear()
        trace, draft = (), None
        calls_before = len(recording.calls)
        started = perf_counter()
        try:
            for event in qa.stream(question.text, search, recording, top_k, record_timing=record_timing):
                if event.kind == "sources":
                    trace = event.trace
                elif event.kind == "delta":
                    if result.first_text_ms is None and event.text.strip():
                        result.first_text_ms = (perf_counter() - started) * 1000
                    parts.append(event.text)
                else:
                    draft = event.draft
                    trace = draft.trace
        except QaError as error:
            result.failure = f"{error.stage}:{error.cause}"
        finished = perf_counter()
        result.last_attempt_ms = (finished - started) * 1000
        result.execution_ms += result.last_attempt_ms
        result.attempt_history.append({
            "attempt": attempt, "status": draft.status if draft is not None else None,
            "failure": result.failure, "decision": trace[-1].decision if trace else None,
            "elapsed_ms": result.last_attempt_ms, "first_text_ms": result.first_text_ms,
            "stage_ms": dict(result.stage_ms), "model_calls": len(recording.calls) - calls_before,
            "backoff_ms": 0.0,
        })
        # Only upstream unavailability is retried; contract failures such as INVALID_CITATIONS are findings.
        if result.failure is None or not result.failure.endswith(":ModelUnavailable") or attempt == max_attempts:
            break
        backoff_started = perf_counter()
        time.sleep(cooldown * attempt)
        backoff_ms = (perf_counter() - backoff_started) * 1000
        result.backoff_ms += backoff_ms
        result.attempt_history[-1]["backoff_ms"] = backoff_ms
    # Stop at the final Qa attempt, before result processing and support checks.
    result.total_ms = (finished - question_started) * 1000
    if result.first_text_ms is not None:
        result.first_text_ms += (started - question_started) * 1000
    result.model_calls = len(recording.calls)
    result.generate_prompt_chars = next((chars for kind, chars in reversed(recording.calls) if kind == "stream"), None)
    result.answer = draft.answer if draft is not None else "".join(parts)
    if draft is not None:
        result.status = draft.status
    if trace:
        result.decision = trace[-1].decision
        result.relevant_ranks = list(getattr(trace[-1], "relevant", ()))
        for hit in trace[-1].retrieved:
            chunk = chunks[hit.chunk_id]
            result.candidates.append({"rank": hit.rank, "chunk_id": hit.chunk_id, "source": chunk.indexed.source,
                                      "seq": chunk.indexed.seq, "score": round(hit.score, 6)})
    if question.expected_source is not None:
        result.expected_rank = next((candidate["rank"] for candidate in result.candidates
                                     if candidate["source"] == question.expected_source), None)
    rank_texts = {candidate["rank"]: chunks[candidate["chunk_id"]].indexed.chunk.text for candidate in result.candidates}
    if result.answer and result.status != "REFUSED":
        result.cited_ranks = [rank for rank in cited_ranks(result.answer) if rank in rank_texts]
        result.cited_sources = sorted({candidate["source"] for candidate in result.candidates
                                       if candidate["rank"] in result.cited_ranks})
        result.sentences = len(split_sentences(result.answer))
        result.uncited_sentences = sum(not cited_ranks(sentence) for sentence in split_sentences(result.answer))
    if question.expected_source is not None and result.status in ("ANSWERED", "PARTIAL") and result.cited_ranks:
        result.wrong_source = question.expected_source not in result.cited_sources
    if faithfulness and draft is not None and result.status != "REFUSED":
        if pause:
            time.sleep(pause)
        check_faithfulness(result, session, rank_texts, pause=pause, cooldown=cooldown, max_attempts=max_attempts)
    return result


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def summarize(results: list[QuestionResult]) -> dict:
    summary: dict = {"categories": {}, "retry_accounting_version": RETRY_ACCOUNTING_VERSION}
    attempted = [result for result in results if result.attempt_history]
    first_successes = sum(result.attempt_history[0]["status"] is not None
                          and result.attempt_history[0]["failure"] is None for result in attempted)
    eventual_successes = sum(result.status is not None and result.failure is None for result in attempted)
    summary["completion"] = {
        "attempted_questions": len(attempted), "first_attempt_successes": first_successes,
        "eventual_successes": eventual_successes,
        "first_attempt_success_rate": first_successes / len(attempted) if attempted else None,
        "eventual_success_rate": eventual_successes / len(attempted) if attempted else None,
    }
    for category in "QRNP":
        selected = [result for result in results if result.category == category]
        summary["categories"][category] = {
            "total": len(selected),
            "decisions": dict(Counter(result.decision or "FAILED" for result in selected)),
            "statuses": dict(Counter(result.status or (result.failure or "FAILED") for result in selected)),
            "expected_decisions": sum(bool(result.decision_expected) for result in selected),
            "failures": sum(result.failure is not None for result in selected),
        }
    scored = [result for result in results if result.category in "QR"]
    answered = [result for result in scored if result.status in ("ANSWERED", "PARTIAL")]
    summary["sources"] = {
        "scored": len(scored),
        "expected_in_candidates": sum(result.expected_rank is not None for result in scored),
        "answered_with_citations": sum(result.wrong_source is not None for result in answered),
        "wrong_source": sum(bool(result.wrong_source) for result in answered),
        "wrong_source_ids": [result.question_id for result in answered if result.wrong_source],
        "missing_expected_ids": [result.question_id for result in scored if result.expected_rank is None],
    }
    checked = [result for result in results if result.supported is not None]
    supported = sum(result.supported for result in checked)
    unsupported = sum(result.unsupported for result in checked)
    summary["faithfulness"] = {
        "metric_version": FAITHFULNESS_METRIC_VERSION,
        "checked_answers": len(checked),
        "checked_sentences": supported + unsupported,
        "supported": supported, "unsupported": unsupported,
        "invalid": sum(result.support_invalid for result in checked),
        "support_rate": supported / (supported + unsupported) if supported + unsupported else None,
        "sentences": sum(result.sentences for result in results),
        "uncited_sentences": sum(result.uncited_sentences for result in results),
        "meta_sentences": sum(result.meta_sentences for result in results),
        "unsupported_ids": [result.question_id for result in checked if result.unsupported],
    }
    generated = [result for result in results if result.first_text_ms is not None]
    compressed = [result for result in results if result.decision in ("SUFFICIENT", "PARTIAL") and result.candidates]

    def expected_dropped(result: QuestionResult) -> bool:
        expected_ranks = {candidate["rank"] for candidate in result.candidates if candidate["source"] == result.expected_source}
        return bool(expected_ranks and result.relevant_ranks) and expected_ranks.isdisjoint(result.relevant_ranks)

    summary["evidence"] = {
        "answers": len(compressed),
        "candidates": sum(len(result.candidates) for result in compressed),
        "relevant": sum(len(result.relevant_ranks) for result in compressed),
        "subset_answers": sum(0 < len(result.relevant_ranks) < len(result.candidates) for result in compressed),
        "expected_dropped_ids": [result.question_id for result in compressed if expected_dropped(result)],
    }
    summary["latency_ms"] = {
        "first_text_median": _median([result.first_text_ms for result in generated]),
        "first_text_max": max((result.first_text_ms for result in generated), default=None),
        "total_median": _median([result.total_ms for result in results]),
        "total_max": max((result.total_ms for result in results), default=None),
        "last_attempt_median": _median([result.last_attempt_ms for result in results]),
        "last_attempt_max": max((result.last_attempt_ms for result in results), default=None),
        "execution_median": _median([result.execution_ms for result in results]),
        "execution_max": max((result.execution_ms for result in results), default=None),
        "backoff_median": _median([result.backoff_ms for result in results]),
        "backoff_max": max((result.backoff_ms for result in results), default=None),
        "refusal_total_median": _median([result.total_ms for result in results if result.status == "REFUSED"]),
        "stages_median": {stage: _median([result.stage_ms[stage] for result in results if stage in result.stage_ms])
                          for stage in ("embedding", "vector", "judge", "generate")},
        "generate_prompt_chars_median": _median([result.generate_prompt_chars for result in results
                                                 if result.generate_prompt_chars is not None]),
        "answer_chars_median": _median([len(result.answer) for result in generated]),
    }
    summary["calls"] = {
        "question_calls": sum(result.model_calls for result in results),
        "support_calls": sum(result.support_calls for result in results),
        "support_sentences": sum(result.support_sentences for result in results),
        "retried_questions": sum(result.attempts > 1 for result in results),
        "unavailable_after_retries": sum(bool(result.failure and result.failure.endswith(":ModelUnavailable"))
                                         for result in results),
    }
    return summary


def _format_ms(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f}"


def _format_rate(numerator: int, denominator: int) -> str:
    return f"{numerator}/{denominator}" + (f"（{numerator / denominator:.1%}）" if denominator else "")


def render_report(report: dict) -> str:
    summary = report["summary"]
    parameters = report["parameters"]
    categories = summary["categories"]
    sources = summary["sources"]
    evidence = summary["evidence"]
    faith = summary["faithfulness"]
    latency = summary["latency_ms"]
    completion = summary["completion"]
    lines = [
        "# 在线问答验收报告（子 Issue D）", "",
        f"> 运行时间：{report['generated_at']}。真实判定/生成模型：{parameters['llm_provider']} / `{parameters['llm_model']}`；"
        f"检索策略：{parameters['retrieval_strategy']}；embedding：`{parameters['embedding_model']}` / {parameters['embedding_dim']} 维；"
        f"top_k={parameters['top_k']}；引用支持率核对：{'开启' if parameters['faithfulness'] else '关闭'}。", "",
        "## 验收边界", "",
        "样例语料在纯 ASCII 临时目录建隔离索引，经 B 的真实切片/embedding 与 C 的真实判定/生成得到结果；"
        "不读取业务 MySQL，不打开业务 Chroma，不写入历史。模型输出每次运行可能不同，数字是本次样本，不是概率保证。",
        "拒答正确率、错源率与引用支持率都由本脚本在线得到；离线召回报告不能替代它们。", "",
        "## 判定与状态", "",
        "| 类别 | 题数 | 期望判定 | 判定符合 | 判定分布 | 状态分布 | 技术失败 |", "|---|---:|---|---:|---|---|---:|",
    ]
    for category in "QRNP":
        metric = categories[category]
        lines.append(
            f"| {category}（{CATEGORY_LABELS[category]}） | {metric['total']} | {' / '.join(EXPECTED_DECISIONS[category])} | "
            f"{_format_rate(metric['expected_decisions'], metric['total'])} | {_cell(metric['decisions'])} | "
            f"{_cell(metric['statuses'])} | {metric['failures']} |"
        )
    lines += [
        "", "N 类“判定符合”即拒答正确率；Q/R 类不符合即误拒；P 类判 SUFFICIENT 记为冒充完整、判 NONE 记为误拒。技术失败（如 INVALID_CITATIONS）不计入拒答。", "",
        f"首次技术成功：{_format_rate(completion['first_attempt_successes'], completion['attempted_questions'])}；"
        f"最终技术成功：{_format_rate(completion['eventual_successes'], completion['attempted_questions'])}。"
        "分母为有尝试记录的题数；正常作答、部分作答和拒答均算技术成功，不表示回答正确。", "",
        "## 出处", "",
        f"- 标注出处进入候选（Q+R）：{_format_rate(sources['expected_in_candidates'], sources['scored'])}；未进入：{', '.join(sources['missing_expected_ids']) or '无'}。",
        f"- 错源率（已作答且有引用的 Q/R 中，引用片段全部不属于标注出处）：{_format_rate(sources['wrong_source'], sources['answered_with_citations'])}；"
        f"错源题：{', '.join(sources['wrong_source_ids']) or '无'}。", "",
        "错源的答案引用合法、内容也可能没有编造，但答的不是用户资料里的那一篇；判定器与引用校验都抓不住它，只有检索层能防。", "",
        "## 证据压缩", "",
        f"- 作答的 {evidence['answers']} 题共 {evidence['candidates']} 个候选，判定器标为相关并送入生成的 {evidence['relevant']} 个"
        f"（{evidence['relevant'] / evidence['candidates']:.1%}）；只送子集的答案 {evidence['subset_answers']} 题。"
        if evidence["candidates"] else "- 本次没有作答的题，无压缩数据。",
        f"- 标注出处进了候选却被判定器排除的题：{', '.join(evidence['expected_dropped_ids']) or '无'}。", "",
        "判定器未输出 relevant 时按全部候选计，因此比例既含模型主动压缩也含未压缩。", "",
        "## 引用支持率", "",
    ]
    if parameters["faithfulness"]:
        rate = faith["support_rate"]
        lines += [
            f"- 指标口径：`{faith['metric_version']}`；仅整句匹配纯说明模板时跳过，混合事实或不确定句仍核对。",
            f"- 核对答案数：{faith['checked_answers']}；带引用的句子：支持 {faith['supported']}、不支持 {faith['unsupported']}、核对失败或输出无效 {faith['invalid']}。",
            f"- 引用支持率：{'—' if rate is None else f'{rate:.1%}'}；存在不支持句子的题：{', '.join(faith['unsupported_ids']) or '无'}。",
            f"- 分母为取得有效判官结果的带引用句：{faith['checked_sentences']}（支持 + 不支持）；核对失败或输出无效不进分母。",
            f"- 句子总数 {faith['sentences']}，其中无引用句 {faith['uncited_sentences']}（不核对，只计数），"
            f"带引用且整句匹配的纯元话语句 {faith['meta_sentences']}（如“以上回答严格基于片段”，不核对）。",
            "", "核对方式：答案去掉围栏代码与列表/标题标记后按句号/问号/感叹号/换行切句，以冒号结尾的引导句和纯加粗标题不算句子；"
            "每个带 [n] 的非元话语句连同所引片段交给同一模型判断“是否完全由片段支持”。它衡量的是生成是否越出片段，不衡量答案是否正确；"
            "引用编号合法不等于语义支持；判官可能双向误判，未经人工校准不能作为严格下界。"
            "本口径不覆盖或重算旧报告，不能与旧关键词过滤口径直接比较。",
        ]
    else:
        lines.append("本次未开启 `--faithfulness`，未核对句子是否被片段支持。")
    lines += [
        "", "## 耗时（毫秒）", "",
        f"计时与调用口径：`{summary['retry_accounting_version']}`。", "",
        "| 指标 | 中位数 | 最大值 |", "|---|---:|---:|",
        f"| 最终尝试首段非空文本（含此前尝试与退避） | {_format_ms(latency['first_text_median'])} | {_format_ms(latency['first_text_max'])} |",
        f"| 整题总等待（问答流程） | {_format_ms(latency['total_median'])} | {_format_ms(latency['total_max'])} |",
        f"| 最后一次尝试 | {_format_ms(latency['last_attempt_median'])} | {_format_ms(latency['last_attempt_max'])} |",
        f"| 累计执行（全部尝试） | {_format_ms(latency['execution_median'])} | {_format_ms(latency['execution_max'])} |",
        f"| 实际退避 | {_format_ms(latency['backoff_median'])} | {_format_ms(latency['backoff_max'])} |",
        f"| 拒答整题总等待 | {_format_ms(latency['refusal_total_median'])} | — |",
    ]
    for stage, label in (("embedding", "问题 embedding"), ("vector", "向量检索"), ("judge", "判定调用"), ("generate", "生成调用")):
        lines.append(f"| {label}（最后一次尝试） | {_format_ms(latency['stages_median'][stage])} | — |")
    lines += [
        "", f"- 生成提示字符数中位数：{_format_ms(latency['generate_prompt_chars_median'])}；答案字符数中位数：{_format_ms(latency['answer_chars_median'])}。",
        f"- 模型调用：问答 {summary['calls']['question_calls']} 次（判定 + 生成），引用核对 {summary['calls']['support_sentences']} 句、"
        f"实际调用 {summary['calls']['support_calls']} 次（调用次数均含失败和重试）；"
        f"因上游不可用重试过的题 {summary['calls']['retried_questions']}，重试后仍不可用 {summary['calls']['unavailable_after_retries']}。",
        "- 计时从进入问答重试循环至最后一次 Qa 尝试结束，包含检索、模型调用、实际退避及循环编排；"
        "不含会话创建、逐句支持核对、题间停顿、产品 HTTP 入口、历史写库与浏览器渲染。不是浏览器端到端延迟。"
        "首字取最终尝试出现文本的时刻；失败尝试已丢弃的文本时刻保留在逐次记录中。", "",
        "## 参数与数据", "",
        f"- 语料：{report['document_count']} 篇、{report['chunk_count']} 个片段；语料指纹：`{report['corpus_sha256']}`。",
        f"- 黄金集：{'；'.join(f'`{name}` SHA-256 `{digest}`' for name, digest in report['golden_sha256'].items())}。",
        f"- 切片：max_tokens={parameters['max_tokens']}；min_tokens={parameters['min_tokens']}；tokenizer SHA-256 `{parameters['tokenizer_sha256']}`。",
        f"- 索引构建（真实 embedding）：{report['index_seconds']:.1f} 秒。",
        "", "## 复跑", "",
        "在仓库的 `rag-service/` 目录执行；需要本地 Ollama 已拉取 embedding 模型，且 `.env` 或 `config/llm.json` 配好回答模型。", "",
        "```powershell", "$env:PYTHONIOENCODING = 'utf-8'", report["command"], "```", "",
        "## 逐题明细", "",
        "| 题号 | 类别 | 判定 | 状态 | 标注出处名次 | 引用名次 | 错源 | 支持/不支持 | 最终首字总等待 ms | 整题总等待 ms | 答案开头 |",
        "|---|---|---|---|---:|---|---|---|---:|---:|---|",
    ]
    for result in report["results"]:
        support = "—" if result["supported"] is None else f"{result['supported']}/{result['unsupported']}"
        wrong = "—" if result["wrong_source"] is None else ("是" if result["wrong_source"] else "否")
        lines.append(
            f"| {result['question_id']} | {result['category']} | {result['decision'] or '—'} | "
            f"{result['status'] or result['failure'] or '—'} | {result['expected_rank'] if result['expected_rank'] else '—'} | "
            f"{','.join(map(str, result['cited_ranks'])) or '—'} | {wrong} | {support} | {_format_ms(result['first_text_ms'])} | "
            f"{_format_ms(result['total_ms'])} | {_cell(result['answer'][:60])} |"
        )
    lines += ["", "## 逐次尝试", "",
              "| 题号 | 尝试 | 结果 | 执行 ms | 本次首字 ms | 模型调用 | 后续退避 ms |",
              "|---|---:|---|---:|---:|---:|---:|"]
    for result in report["results"]:
        for attempt in result["attempt_history"]:
            lines.append(
                f"| {result['question_id']} | {attempt['attempt']} | {attempt['failure'] or attempt['status'] or '—'} | "
                f"{_format_ms(attempt['elapsed_ms'])} | {_format_ms(attempt['first_text_ms'])} | "
                f"{attempt['model_calls']} | {_format_ms(attempt['backoff_ms'])} |"
            )
    lines += ["", "候选、完整答案和逐次分段耗时见同名 `.json`。", "", "## 环境与代码指纹", ""]
    lines.extend(f"- {package}: `{installed}`" for package, installed in report["versions"].items())
    lines.append("")
    lines.extend(f"- `{source}` SHA-256: `{digest}`" for source, digest in report["code_sha256"].items())
    return "\n".join(lines) + "\n"


def _selected(questions: list[Question], only: str | None) -> list[Question]:
    if not only:
        return questions
    tokens = {token.strip() for token in only.split(",") if token.strip()}
    return [question for question in questions if question.question_id in tokens or question.category in tokens]


def build_index(retrieval: Retrieval, documents: list[tuple[int, list[EvaluationChunk]]]) -> None:
    for document_id, chunks in documents:
        retrieval.replace(document_id, index_representatives(
            IndexChunk(chunk.chunk_id, chunk.indexed.chunk.text, chunk.indexed.chunk.heading_path) for chunk in chunks
        ))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="子 Issue D：隔离索引 + 真实判定/生成模型的在线问答验收")
    parser.add_argument("--corpus", type=Path, default=PROJECT_ROOT / "sample-knowledge")
    parser.add_argument("--golden-set", type=Path, nargs="+", default=list(DEFAULT_GOLDEN_SETS))
    parser.add_argument("--tokenizer", type=Path, default=SERVICE_DIR / "data/tokenizers" / f"bge-m3-{TOKENIZER_REVISION}.json")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--model", default="bge-m3")
    parser.add_argument("--dimensions", type=int, default=1024)
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--min-tokens", type=int, default=64)
    parser.add_argument("--timeout-seconds", type=float, default=180)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--strategy", choices=("dense", "hybrid"), help="检索策略；默认与服务相同")
    parser.add_argument("--faithfulness", action="store_true")
    parser.add_argument("--pause", type=float, default=1.0, help="相邻模型调用之间的停顿秒数，避免触发上游限流")
    parser.add_argument("--cooldown", type=float, default=15.0, help="上游不可用时的退避基数秒数（乘以尝试次数）")
    parser.add_argument("--max-attempts", type=int, default=3, help="上游不可用时同一题/同一句最多尝试次数")
    parser.add_argument("--only", help="逗号分隔的题号或类别，例如 N,P 或 Q21,N8")
    parser.add_argument("--config", type=Path, help="回答模型配置文件；默认与服务相同")
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args(argv)
    if arguments.dimensions <= 0 or arguments.timeout_seconds <= 0 or arguments.top_k <= 0:
        parser.error("dimensions, timeout-seconds and top-k must be positive")
    if arguments.pause < 0 or arguments.cooldown < 0 or arguments.max_attempts < 1:
        parser.error("pause and cooldown must be non-negative; max-attempts must be at least 1")
    if not arguments.tokenizer.is_file():
        parser.error(f"tokenizer file missing; default tokenizer download: {TOKENIZER_URL}")

    tokenizer = load_tokenizer(arguments.tokenizer)
    count_tokens = lambda text: len(tokenizer.encode(text, add_special_tokens=True).ids)
    questions: list[Question] = []
    golden_sha256 = {}
    for path in arguments.golden_set:
        golden_bytes = path.read_bytes()
        golden_sha256[path.name] = hashlib.sha256(golden_bytes).hexdigest()
        questions.extend(parse_questions(golden_bytes.decode("utf-8")))
    if len({question.question_id for question in questions}) != len(questions):
        parser.error("question ids must be unique across golden sets")
    questions = _selected(questions, arguments.only)
    records, manifest = prepare_corpus(arguments.corpus, count_tokens=count_tokens,
                                       max_tokens=arguments.max_tokens, min_tokens=arguments.min_tokens)
    if not questions or not records:
        parser.error("evaluation requires questions and nonempty Markdown chunks")
    sources = {record.source for record in records}
    missing = [question.question_id for question in questions
               if question.expected_source is not None and question.expected_source not in sources]
    if missing:
        parser.error(f"expected sources missing from corpus: {', '.join(missing)}")

    document_ids = {source: index for index, source in enumerate(sorted(sources), start=1)}
    chunks: dict[int, EvaluationChunk] = {}
    documents: dict[int, list[EvaluationChunk]] = {}
    for chunk_id, record in enumerate(records, start=1):
        chunk = EvaluationChunk(chunk_id, document_ids[record.source], record)
        chunks[chunk_id] = chunk
        documents.setdefault(chunk.document_id, []).append(chunk)

    temporary = Path(tempfile.mkdtemp(prefix="easyrag-eval-"))
    if not str(temporary).isascii():
        shutil.rmtree(temporary, ignore_errors=True)
        parser.error("temporary directory must be an ASCII path (B-16); set TEMP to an ASCII directory")
    settings = RetrievalSettings(
        _env_file=None, chroma_dir=temporary / "chroma", chunk_tokenizer_path=arguments.tokenizer,
        chunk_max_tokens=arguments.max_tokens, chunk_min_tokens=arguments.min_tokens,
        embedding_provider="ollama", embedding_model=arguments.model, embedding_dim=arguments.dimensions,
        embedding_base_url=arguments.ollama_url, embedding_timeout_seconds=arguments.timeout_seconds,
        **({"retrieval_strategy": arguments.strategy} if arguments.strategy else {}),
    )
    retrieval = Retrieval(settings)
    results: list[QuestionResult] = []
    try:
        health = retrieval.health()
        if health["status"] != "UP":
            parser.error(f"retrieval dependencies are not ready: {json.dumps(health, ensure_ascii=False)}")
        models = Models(arguments.config)
        models.prepare()
        print(f"Indexing {len(records)} chunks ({len(documents)} documents)", file=sys.stderr, flush=True)
        started = perf_counter()
        build_index(retrieval, sorted(documents.items()))
        index_seconds = perf_counter() - started
        qa = Qa()
        model_info = None
        for position, question in enumerate(questions, start=1):
            if position > 1 and arguments.pause:
                time.sleep(arguments.pause)
            session = models.open_session()
            model_info = model_info or session.model_info
            print(f"[{position}/{len(questions)}] {question.question_id}", file=sys.stderr, flush=True)
            results.append(evaluate_question(qa, question, retrieval, session, arguments.top_k, chunks,
                                             faithfulness=arguments.faithfulness, pause=arguments.pause,
                                             cooldown=arguments.cooldown, max_attempts=arguments.max_attempts))
            latest = results[-1]
            print(f"    {latest.decision or '-'} {latest.status or latest.failure or '-'} "
                  f"{latest.total_ms:.0f}ms attempts={latest.attempts}", file=sys.stderr, flush=True)
    finally:
        retrieval.close()
        shutil.rmtree(temporary, ignore_errors=True)

    summary = summarize(results)
    tokenizer_digest = hashlib.sha256(arguments.tokenizer.read_bytes()).hexdigest()
    tokenizer_argument = (f"data/tokenizers/bge-m3-{TOKENIZER_REVISION}.json" if tokenizer_digest == TOKENIZER_SHA256
                          else "<tokenizer.json>")
    command = [".\\.venv\\Scripts\\python.exe -m scripts.eval_answers",
               "--golden-set " + " ".join(_quote("../docs/eval/" + path.name) for path in arguments.golden_set),
               f"--tokenizer {_quote(tokenizer_argument)}", f"--model {_quote(arguments.model)}",
               f"--dimensions {arguments.dimensions}", f"--top-k {arguments.top_k}",
               f"--strategy {settings.retrieval_strategy}"]
    if arguments.faithfulness:
        command.append("--faithfulness")
    command.append(f"--pause {arguments.pause} --cooldown {arguments.cooldown} --max-attempts {arguments.max_attempts}")
    if arguments.only:
        command.append(f"--only {_quote(arguments.only)}")
    if arguments.output:
        command.append(f"--output {_quote('../docs/eval/' + arguments.output.name)}")
    report = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "parameters": {
            "llm_provider": model_info["provider"] if model_info else None,
            "llm_model": model_info["model"] if model_info else None,
            "retrieval_strategy": settings.retrieval_strategy,
            "embedding_model": arguments.model, "embedding_dim": arguments.dimensions,
            "top_k": arguments.top_k, "faithfulness": arguments.faithfulness, "only": arguments.only,
            "pause": arguments.pause, "cooldown": arguments.cooldown, "max_attempts": arguments.max_attempts,
            "max_tokens": arguments.max_tokens, "min_tokens": arguments.min_tokens,
            "tokenizer_sha256": tokenizer_digest,
        },
        "document_count": len(documents), "chunk_count": len(records), "index_seconds": index_seconds,
        "corpus_sha256": hashlib.sha256("".join(f"{item['source']}\0{item['sha256']}\n" for item in manifest).encode("utf-8")).hexdigest(),
        "golden_sha256": golden_sha256,
        "results": [asdict(result) for result in results], "summary": summary,
        "versions": {"Python": platform.python_version(),
                     **{package: version(package) for package in ("chromadb", "tokenizers", "langchain", "langchain-openai")}},
        "code_sha256": {
            "retrieval.split_markdown": splitter_fingerprint(),
            **{source: hashlib.sha256((SERVICE_DIR / source).read_bytes()).hexdigest()
               for source in ("app/modules/qa/public.py", "scripts/eval_answers.py", "scripts/eval_retrieval.py")},
        },
        "command": " `\n  ".join(command),
    }
    rendered = render_report(report)
    if arguments.output:
        arguments.output.write_text(rendered, encoding="utf-8", newline="\n")
        arguments.output.with_suffix(".json").write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n",
        )
        print(f"Report: {arguments.output}")
    else:
        print(rendered)
    for category in "QRNP":
        metric = summary["categories"][category]
        print(f"{category}: expected decisions {metric['expected_decisions']}/{metric['total']}, failures {metric['failures']}")
    print(f"wrong source: {summary['sources']['wrong_source']}/{summary['sources']['answered_with_citations']}; "
          f"support rate: {summary['faithfulness']['support_rate']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
