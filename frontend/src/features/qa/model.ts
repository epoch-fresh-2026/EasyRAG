import type { AnsweredQuestion, ChunkSource, TraceEntry } from '@/shared/api/types'

export interface Citation {
  number: number
  source: ChunkSource
}

export type AnswerSegment = { type: 'text'; text: string } | { type: 'citation'; citation: Citation }

export interface AnswerPresentation {
  segments: AnswerSegment[]
  tokens: Token[]
  citations: Citation[]
  retrievedCount: number
}

export function retrievalLabel(round: TraceEntry, rank: number, reference?: number): string {
  if (round.relevant === undefined) return reference ? `正文引用 ${reference}` : '检索结果'
  if (!round.relevant.includes(rank)) return '未进生成'
  return reference ? `采用 · 正文引用 ${reference}` : '采用'
}

export function presentAnswer(result: AnsweredQuestion): AnswerPresentation {
  const sources = new Map(result.sources.map((source) => [source.chunk_id, source]))
  const byNumber = new Map<number, Citation>()
  for (const entry of result.trace.at(-1)?.retrieved ?? []) {
    const source = sources.get(entry.chunk_id)
    if (source) byNumber.set(entry.rank, { number: entry.rank, source })
  }

  const segments = parseCitationText(result.answer, byNumber)
  const tokens = Lexer.lex(result.answer)
  const used = new Map<number, Citation>()
  function collect(nodes: Token[]) {
    for (const token of nodes) {
      if (['code', 'codespan', 'html', 'image', 'link', 'escape'].includes(token.type)) continue
      if (token.tokens) collect(token.tokens)
      else if (token.type === 'text') {
        for (const segment of parseCitationText(token.text, byNumber)) {
          if (segment.type === 'citation') used.set(segment.citation.number, segment.citation)
        }
      } else if (token.type === 'list') {
        for (const item of token.items as Tokens.ListItem[]) collect(item.tokens)
      } else if (token.type === 'table') {
        const table = token as Tokens.Table
        for (const cell of [...table.header, ...table.rows.flat()]) collect(cell.tokens)
      }
    }
  }
  collect(tokens)
  return {
    segments,
    tokens,
    citations: [...used.values()].sort((left, right) => left.number - right.number),
    retrievedCount: new Set(result.trace.flatMap((round) => round.retrieved.map((entry) => entry.chunk_id))).size,
  }
}

export function parseCitationText(text: string, byNumber: Map<number, Citation>): AnswerSegment[] {
  const segments: AnswerSegment[] = []
  const referencePattern = /\[(\d+(?:\s*(?:[,，]|[-–])\s*\d+)*)\]/g
  let cursor = 0
  let plainText = ''

  for (const match of text.matchAll(referencePattern)) {
    plainText += text.slice(cursor, match.index)
    const numbers = resolveReferenceNumbers(match[1]!, byNumber)
    if (numbers.length === 0) {
      plainText += match[0]
    } else {
      if (plainText) segments.push({ type: 'text', text: plainText })
      plainText = ''
      for (const number of numbers) {
        const citation = byNumber.get(number)!
        segments.push({ type: 'citation', citation })
      }
    }
    cursor = match.index + match[0].length
  }
  plainText += text.slice(cursor)
  if (plainText) segments.push({ type: 'text', text: plainText })

  return segments
}

function resolveReferenceNumbers(group: string, available: Map<number, Citation>): number[] {
  const resolved: number[] = []
  for (const part of group.split(/[,，]/)) {
    const range = part.trim().split(/[-–]/).map(Number)
    const start = range[0]!
    if (range.length === 1) {
      if (!available.has(start)) return []
      resolved.push(start)
    } else {
      const end = range[1]!
      const numbers = [...available.keys()].filter((number) => number >= start && number <= end).sort((a, b) => a - b)
      if (end < start || numbers.length !== end - start + 1) return []
      resolved.push(...numbers)
    }
  }
  return [...new Set(resolved)]
}
import { Lexer, type Token, type Tokens } from 'marked'
