<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { useDocumentStore } from '@/features/documents/store'
import type { AnsweredQuestion } from '@/shared/api/types'
import AppIcon from '@/shared/components/AppIcon.vue'
import { useGateStore } from '@/shared/gate'
import { presentAnswer, retrievalLabel } from '../model'
import AnswerBody from './AnswerBody'

const props = defineProps<{ result: AnsweredQuestion; question: string; elapsedMs: number; model: string; createdAt: string; stale: boolean; history: boolean; retryDisabled: boolean }>()
defineEmits<{ retry: [] }>()
const router = useRouter()
const route = useRoute()
const library = useDocumentStore()
const gate = useGateStore()
const presentation = computed(() => presentAnswer(props.result))
const selected = ref<number | null>(null)
const copyMessage = ref('')
const decisionLabels: Record<string, string> = { SUFFICIENT: '资料足以回答', PARTIAL: '资料只覆盖部分问题', NONE: '尚无足够依据' }

function jumpToSource(number: number) {
  selected.value = number
  const card = document.getElementById(`citation-${number}`)
  card?.scrollIntoView({ behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth', block: 'center' })
  card?.focus({ preventScroll: true })
}

function openDocument(documentId: number, chunkId?: number) {
  void router.push({ query: { ...route.query, document: String(documentId), ...(chunkId ? { chunk: String(chunkId) } : {}) } })
}

function sourceTitle(documentId: number): string {
  return props.result.sources.find((source) => source.document_id === documentId)?.title
    ?? library.items.find((document) => document.id === documentId)?.title ?? `资料 #${documentId}`
}

function savedSource(chunkId: number) {
  return props.result.sources.find((source) => source.chunk_id === chunkId)
}

function referenceNumber(chunkId: number): number | undefined {
  return presentation.value.citations.find((citation) => citation.source.chunk_id === chunkId)?.number
}

async function copyAnswer() {
  try {
    await navigator.clipboard.writeText(props.result.answer)
    copyMessage.value = '已复制'
  } catch {
    copyMessage.value = '复制失败，请选择正文复制'
  }
}
</script>

<template>
  <section class="answer-section page-enter" aria-label="知识库回答">
    <div v-if="history" class="history-answer-context">
      <div><span class="eyebrow">历史回答</span><p>以下出处保留提问时的内容。</p></div>
      <button class="text-button history-reask" :disabled="retryDisabled || !gate.canAsk" @click="$emit('retry')"><AppIcon name="refresh" :size="15" />重新提问</button>
      <p class="history-reask-hint">重新提问将使用当前资料与模型，并另存一条记录。</p>
    </div>
    <div class="answer-question"><span class="question-label">你的问题</span><h2>{{ question }}</h2></div>

    <article class="answer-paper" :class="{ 'answer-refused': result.status === 'REFUSED' }">
      <div class="answer-topline"><span class="answer-label"><span class="answer-symbol"><AppIcon name="spark" :size="17" /></span>知识库回答</span><span class="answer-status" :class="result.status.toLowerCase()"><span class="status-dot" />{{ result.status === 'PARTIAL' ? '部分覆盖' : result.status === 'REFUSED' ? '依据不足' : '已找到依据' }}</span></div>
      <div v-if="stale" class="notice notice-warm answer-notice"><AppIcon name="refresh" :size="17" /><span>引用资料已发生变化。这是更新前的回答，请重新提问以获取最新内容。</span><button class="text-button" :disabled="retryDisabled || !gate.canAsk" @click="$emit('retry')">重新提问</button></div>
      <div v-if="result.status === 'PARTIAL'" class="coverage-note"><AppIcon name="info" :size="18" /><div><strong>现有资料只能回答部分问题</strong><span>以下回答以已找到的内容为依据，未覆盖的部分会在正文中说明。</span></div></div>

      <div v-if="result.status === 'REFUSED'" class="refusal-intro"><span class="refusal-symbol"><AppIcon name="book" :size="28" /></span><h3>这次，还没有找到足够的依据。</h3></div>
      <AnswerBody :tokens="presentation.tokens" :citations="presentation.citations" @citation="jumpToSource" />
      <RouterLink v-if="result.status === 'REFUSED'" to="/" class="button button-outlined refusal-action"><AppIcon name="plus" :size="16" />添加相关资料</RouterLink>

      <div class="answer-bottomline"><span class="answer-record-meta"><span class="answer-meta-line"><AppIcon name="clock" :size="13" />{{ (elapsedMs / 1000).toFixed(1) }} 秒<span class="answer-model" :title="model">{{ model }}</span></span><time class="answer-saved-at" :datetime="createdAt" title="北京时间">保存于 {{ createdAt.replace('T', ' ').slice(0, 19) }}</time></span><button class="text-button copy-button" @click="copyAnswer"><AppIcon name="copy" :size="14" />{{ copyMessage || '复制回答' }}</button></div>
    </article>

    <section v-if="presentation.citations.length" class="citations-section" aria-labelledby="citations-heading">
      <div class="citations-heading"><h3 id="citations-heading"><AppIcon name="quote" :size="15" />回答的出处</h3><span>{{ presentation.citations.length }} {{ history ? '个保存片段 · 提问时的来源全文' : '个引用片段 · 点击核验原文' }}</span></div>
      <div class="citation-grid" :class="{ 'citation-snapshots': history }">
        <article v-for="citation in presentation.citations" :id="`citation-${citation.number}`" :key="citation.number" class="source-card" :class="{ 'is-highlighted': selected === citation.number }" tabindex="-1">
          <div class="source-card-header"><span class="citation-number">{{ citation.number }}</span><span v-if="history" class="source-title snapshot-title">{{ citation.source.title }}</span><button v-else class="source-title" @click="openDocument(citation.source.document_id, citation.source.chunk_id)">{{ citation.source.title }}</button><AppIcon name="file" :size="16" /></div>
          <p v-if="citation.source.heading_path" class="source-heading-path">{{ citation.source.heading_path }}</p>
          <p v-if="history" class="source-snapshot">{{ citation.source.text }}</p>
          <template v-else><p class="source-excerpt">{{ citation.source.text }}</p><button class="source-open" @click="openDocument(citation.source.document_id, citation.source.chunk_id)">查看原文<AppIcon name="arrow" :size="14" /></button></template>
        </article>
      </div>
    </section>

    <details class="trace-details">
      <summary><span class="trace-summary-title"><AppIcon name="search" :size="16" />查看检索过程</span><span class="trace-summary-meta">{{ result.trace.length }} 轮检索<span>·</span>{{ presentation.retrievedCount }} 个片段<AppIcon name="down" :size="15" /></span></summary>
      <div class="trace-content">
        <div class="trace-explanation"><AppIcon name="info" :size="15" /><span>标为“采用”的片段已提供给回答模型；“正文引用”表示答案实际标注的出处。<template v-if="history">这里展示当时的记录；已保存正文的片段可以展开阅读。</template></span></div>
        <section v-for="round in result.trace" :key="round.round_index" class="trace-round">
          <div class="trace-round-heading"><span class="round-number">{{ String(round.round_index).padStart(2, '0') }}</span><strong>{{ decisionLabels[round.decision] }}</strong><span>{{ round.retrieved.length }} 个片段</span></div>
          <p class="trace-query"><span>检索问题</span>{{ round.query }}</p>
          <p v-if="round.relevant === undefined" class="trace-no-results trace-adoption-unavailable">此记录未保存片段采用情况。</p>
          <ul class="retrieval-list">
            <li v-for="entry in round.retrieved" :key="entry.chunk_id" :class="{ 'is-historical': history }">
              <span class="retrieval-rank">{{ entry.rank }}</span>
              <template v-if="history">
                <details v-if="savedSource(entry.chunk_id)" class="retrieval-snapshot"><summary>{{ savedSource(entry.chunk_id)?.title }}<AppIcon name="down" :size="13" /></summary><p v-if="savedSource(entry.chunk_id)?.heading_path" class="source-heading-path">{{ savedSource(entry.chunk_id)?.heading_path }}</p><p class="source-snapshot">{{ savedSource(entry.chunk_id)?.text }}</p></details>
                <span v-else class="retrieval-unavailable">资料 #{{ entry.document_id }} · 片段 #{{ entry.chunk_id }}<small>未保存该片段正文</small></span>
              </template>
              <button v-else @click="openDocument(entry.document_id, entry.chunk_id)">{{ sourceTitle(entry.document_id) }}</button>
              <span class="retrieval-label" :class="{ cited: referenceNumber(entry.chunk_id) }">{{ retrievalLabel(round, entry.rank, referenceNumber(entry.chunk_id)) }}</span>
            </li>
          </ul>
          <p v-if="!round.retrieved.length" class="trace-no-results">本轮没有检索到相关片段。</p>
        </section>
      </div>
    </details>
  </section>
</template>
