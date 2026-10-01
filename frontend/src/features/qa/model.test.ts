import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

import type { AnsweredQuestion, ChunkSource } from '@/shared/api/types'
import { presentAnswer, retrievalLabel } from './model'

const citationCases = JSON.parse(readFileSync(new URL('../../../../rag-service/tests/contracts/answer-citations.json', import.meta.url), 'utf8')) as { id: string; answer: string; numbers: number[] }[]

function source(id: number): ChunkSource {
  return { chunk_id: id, document_id: id, title: `资料 ${id}`, text: `原文 ${id}`, byte_start: 0, byte_end: 8, heading_path: '说明' }
}

function result(answer: string): AnsweredQuestion {
  return {
    answer, status: 'ANSWERED', sources: [source(2), source(9), source(12)],
    trace: [{ round_index: 1, query: '问题', decision: 'SUFFICIENT', retrieved: [
      { chunk_id: 9, document_id: 9, rank: 1, score: 0.1 },
      { chunk_id: 2, document_id: 2, rank: 2, score: 0.2 },
      { chunk_id: 12, document_id: 12, rank: 3, score: 0.3 },
    ] }],
  }
}

describe('answer citations', () => {
  it.each(citationCases)('shares the backend prose citation rules: $id', (testCase) => {
    expect(presentAnswer(result(testCase.answer)).citations.map((citation) => citation.number)).toEqual(testCase.numbers)
  })

  it('resolves citation numbers by retrieval rank, not the order of sources', () => {
    const view = presentAnswer(result('第一项 [1]，第二项 [2]。'))
    expect(view.citations.map((citation) => citation.source.chunk_id)).toEqual([9, 2])
  })

  it('counts actual answer citations separately from all retrieved material', () => {
    const view = presentAnswer(result('结论 [2]。补充 [2]。'))
    expect(view.citations.map((citation) => citation.number)).toEqual([2])
    expect(view.retrievedCount).toBe(3)
    expect(view.segments.filter((segment) => segment.type === 'citation')).toHaveLength(2)
  })

  it('understands grouped and range references emitted by the local model', () => {
    const view = presentAnswer(result('结论 [1-3]。补充 [1, 2]。'))
    expect(view.citations.map((citation) => citation.number)).toEqual([1, 2, 3])
    expect(view.segments.filter((segment) => segment.type === 'citation')).toHaveLength(5)
  })

  it('keeps an unknown reference as text instead of inventing a source', () => {
    const view = presentAnswer(result('结论 [99]。'))
    expect(view.citations).toEqual([])
    expect(view.segments).toEqual([{ type: 'text', text: '结论 [99]。' }])
  })

  it('preserves model output as text, including HTML and markdown', () => {
    const answer = '<img src=x onerror=alert(1)> **说明**\n下一行 [1]'
    const view = presentAnswer(result(answer))
    expect(view.segments[0]).toEqual({ type: 'text', text: '<img src=x onerror=alert(1)> **说明**\n下一行 ' })
  })

  it('does not turn a missing or deleted source into a clickable citation', () => {
    const payload = result('结论 [1]。')
    payload.sources = [source(2)]
    const view = presentAnswer(payload)
    expect(view.citations).toEqual([])
    expect(view.segments).toEqual([{ type: 'text', text: payload.answer }])
  })

  it('maps non-consecutive numbers to sources when the judge kept a subset of candidates', () => {
    const payload = result('第二项 [2]，第三项 [3]，合并 [2-3]。')
    payload.sources = [source(2), source(12)]
    payload.trace[0]!.relevant = [2, 3]
    const view = presentAnswer(payload)
    expect(view.citations.map((citation) => [citation.number, citation.source.chunk_id])).toEqual([[2, 2], [3, 12]])
    expect(view.segments.filter((segment) => segment.type === 'citation')).toHaveLength(4)
    expect(view.retrievedCount).toBe(3)
  })
})

describe('retrieval adoption', () => {
  it('distinguishes excluded, adopted and cited evidence using non-consecutive ranks', () => {
    const round = {
      ...result('').trace[0]!, relevant: [2, 5],
      retrieved: [
        { chunk_id: 2, document_id: 2, rank: 1, score: 0.9 },
        { chunk_id: 9, document_id: 9, rank: 2, score: 0.8 },
        { chunk_id: 12, document_id: 12, rank: 5, score: 0.7 },
      ],
    }
    expect(round.retrieved.map(entry => retrievalLabel(round, entry.rank, entry.rank === 2 ? 2 : undefined)))
      .toEqual(['未进生成', '采用 · 正文引用 2', '采用'])
  })

  it('keeps known citations without inferring adoption for records missing relevant', () => {
    const round = result('').trace[0]!
    expect(retrievalLabel(round, 1, 1)).toBe('正文引用 1')
    expect(retrievalLabel(round, 2)).toBe('检索结果')
  })

  it('treats an explicitly empty relevant list as no adopted evidence', () => {
    const round = { ...result('').trace[0]!, relevant: [], decision: 'NONE' as const }
    expect(round.retrieved.map(entry => retrievalLabel(round, entry.rank)))
      .toEqual(['未进生成', '未进生成', '未进生成'])
  })

  it('uses each round\'s adoption record when the same rank appears in another round', () => {
    const first = { ...result('').trace[0]!, relevant: [1] }
    const second = { ...first, round_index: 2, relevant: [2] }
    expect(retrievalLabel(first, 1)).toBe('采用')
    expect(retrievalLabel(second, 1)).toBe('未进生成')
  })
})
