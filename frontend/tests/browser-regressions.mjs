import assert from 'node:assert/strict'
import { mkdir } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { createServer } from 'vite'

const frontendRoot = fileURLToPath(new URL('../', import.meta.url))
const base = 'http://127.0.0.1:15173'
const server = await createServer({
  root: frontendRoot,
  server: { host: '127.0.0.1', port: 15173, strictPort: true },
  clearScreen: false,
})
let browser
const results = []

async function fixture() {
  const context = await browser.newContext()
  const page = await context.newPage()
  page.setDefaultTimeout(8000)
  const docs = [1, 2].map(id => ({
    id, title: 'Regression document ' + id, content: '# Original ' + id,
    tags: [], source_type: 'UPLOAD', source_uri: 'regression.md', index_status: 'INDEXED',
    index_error: null, chunk_count: 1, created_at: '2026-09-15T12:00:00', updated_at: '2026-09-15T12:00:00',
  }))
  const runtime = {
    state: 'READY', rag_available: true,
    llm: { configured: true, provider: 'openai', model: 'fixture-model' },
    embedding: { provider: 'ollama', model: 'fixture-embedding', dim: 1024 },
  }
  const answer = {
    answer: 'Fixture answer [1]', status: 'ANSWERED',
    history_id: 1, created_at: '2026-09-15T12:00:00',
    model: { provider: 'openai', model: 'fixture-model' }, elapsed_ms: 2400,
    sources: [{ chunk_id: 10, document_id: 1, title: docs[0].title, text: docs[0].content, byte_start: 0, byte_end: 12, heading_path: 'Original' }],
    trace: [{ round_index: 1, query: 'Question', decision: 'SUFFICIENT', retrieved: [{ chunk_id: 10, document_id: 1, rank: 1, score: 0.8 }] }],
  }
  const history = []
  function seedHistory(id, question, changes = {}) {
    const { history_id, ...saved } = structuredClone(answer)
    const record = { ...saved, id, question, ...changes }
    history.push(record)
    return record
  }
  function completeQuestion(route) {
    const id = Math.max(0, ...history.map(record => record.id)) + 1
    const record = seedHistory(id, route.request().postDataJSON().question)
    const { id: savedId, question, ...saved } = record
    return route.fulfill({ contentType: 'text/event-stream', body: `event: done\ndata: ${JSON.stringify({ ...saved, history_id: savedId })}\n\n` })
  }
  function historyPage(url) {
    const page = Number(url.searchParams.get('page'))
    const size = Number(url.searchParams.get('size'))
    const items = [...history].sort((left, right) => right.created_at.localeCompare(left.created_at) || right.id - left.id)
      .slice(page * size, (page + 1) * size)
      .map(({ id, question, status, created_at, model, elapsed_ms }) => ({ id, question, status, created_at, model, elapsed_ms }))
    return { total: history.length, items }
  }
  const logs = { writes: [], confirmDialogs: [], pageErrors: [], externalRequests: [], questions: [], historyReads: [], historyDeletes: [], documentReads: [], knowledgeWrites: [] }
  const controls = {
    acceptDiscard: false, holdWrite: false, mutation: null, onQuestion: null,
    holdHealth: false, healthWaiters: [], afterWrite: null, readyCalls: 0, onUpload: null, failHealth: false,
    onHistoryList: null, onHistoryDetail: null, beforeHistoryDelete: null, onHistoryDelete: null, pendingReleases: [],
  }
  let mutationStarted
  controls.mutationStarted = new Promise(resolve => { mutationStarted = resolve })
  page.on('dialog', async dialog => {
    logs.confirmDialogs.push(dialog.message())
    if (controls.acceptDiscard) await dialog.accept()
    else await dialog.dismiss()
  })
  page.on('pageerror', error => logs.pageErrors.push(error.message))
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url())
    if (url.origin !== base) { logs.externalRequests.push(url.href); return route.abort() }
    if (url.pathname === '/health') {
      if (controls.holdHealth) await new Promise(resolve => { controls.healthWaiters.push(resolve) })
      if (controls.failHealth) { controls.failHealth = false; return route.abort('failed') }
      return route.fulfill({ json: { status: 'UP', db: { status: 'UP' } } })
    }
    if (!url.pathname.startsWith('/api/')) return route.continue()
    if (url.pathname === '/api/runtime') return route.fulfill({ json: runtime })
    if (url.pathname === '/api/admin/ready') {
      controls.readyCalls++
      runtime.state = 'READY'
      return route.fulfill({ json: { state: 'READY', recovered: 0 } })
    }
    if (url.pathname === '/api/questions/stream') {
      logs.questions.push(request.postDataJSON())
      if (controls.onQuestion) return controls.onQuestion(route)
      return completeQuestion(route)
    }
    if (url.pathname === '/api/question-history') {
      logs.historyReads.push(url.search)
      if (controls.onHistoryList) return controls.onHistoryList(route, url)
      return route.fulfill({ json: historyPage(url) })
    }
    const historyMatch = url.pathname.match(/^\/api\/question-history\/(\d+)$/)
    if (historyMatch) {
      const id = Number(historyMatch[1])
      const record = history.find(item => item.id === id)
      if (request.method() === 'DELETE') {
        if (!record) return route.fulfill({ status: 404, json: { error: 'History record no longer exists' } })
        if (controls.beforeHistoryDelete) await controls.beforeHistoryDelete(id)
        history.splice(history.indexOf(record), 1)
        logs.historyDeletes.push(id)
        if (controls.onHistoryDelete) await controls.onHistoryDelete(id)
        return route.fulfill({ status: 204 })
      }
      if (controls.onHistoryDetail) return controls.onHistoryDetail(route, id)
      return record
        ? route.fulfill({ json: record })
        : route.fulfill({ status: 404, json: { error: 'History record no longer exists' } })
    }
    if (url.pathname.startsWith('/api/documents') && request.method() !== 'GET') {
      logs.knowledgeWrites.push({ method: request.method(), path: url.pathname })
    }
    if (url.pathname === '/api/documents') {
      if (request.method() === 'POST') {
        const item = { ...docs[0], id: 3, title: 'Uploaded note', content: '# Uploaded', index_status: 'PENDING' }
        docs.push(item)
        controls.onUpload?.()
        return route.fulfill({ status: 201, json: { id: item.id, title: item.title, source_type: 'UPLOAD', index_status: 'PENDING' } })
      }
      return route.fulfill({ json: { items: docs, total: docs.length } })
    }
    const match = url.pathname.match(/^\/api\/documents\/(\d+)(\/chunks|\/reindex)?$/)
    if (match) {
      if (request.method() === 'GET') logs.documentReads.push(url.pathname)
      const item = docs.find(doc => doc.id === Number(match[1]))
      if (request.method() === 'PUT') {
        const body = request.postDataJSON()
        logs.writes.push({ id: item.id, body })
        if (controls.holdWrite) await new Promise(resolve => { controls.mutation = resolve; mutationStarted() })
        item.content = body.content
        controls.afterWrite?.()
        return route.fulfill({ json: { id: item.id, index_status: 'PENDING', reindexed: true } })
      }
      if (request.method() === 'DELETE') {
        docs.splice(docs.indexOf(item), 1)
        return route.fulfill({ status: 204 })
      }
      if (match[2] === '/reindex') return route.fulfill({ status: 202, json: { id: item.id, index_status: 'PENDING' } })
      if (match[2] === '/chunks') return route.fulfill({ json: { items: [{ id: item.id * 10, seq: 0, text: item.content, byte_start: 0, byte_end: item.content.length, heading_path: 'Original', token_count: 10 }] } })
      return route.fulfill({ json: item })
    }
    return route.fulfill({ status: 404, json: { error: 'Unspecified fixture API' } })
  })
  return { context, page, docs, runtime, answer, history, seedHistory, completeQuestion, historyPage, logs, controls }
}

// Wait for the router to settle even when it rejects browser Back.
async function back(page) {
  await page.evaluate(async () => {
    const { default: router } = await import('/src/app/router.ts')
    await new Promise(resolve => {
      const remove = router.afterEach(() => { remove(); resolve() })
      window.history.back()
    })
  })
}

async function run(name, action) {
  const test = await fixture()
  try {
    await action(test)
    assert.deepEqual(test.logs.pageErrors, [])
    assert.deepEqual(test.logs.externalRequests, [])
    results.push({ name, passed: true })
    console.log('PASS', name)
  } catch (error) {
    results.push({ name, passed: false, error: error.message })
    console.error('FAIL', name, error.message)
  } finally {
    test.controls.holdHealth = false
    for (const release of test.controls.healthWaiters) release()
    test.controls.mutation?.()
    for (const release of test.controls.pendingReleases) release()
    await test.context.close()
  }
}

try {
  await server.listen()
  browser = await chromium.launch({ channel: 'chrome', headless: true })

  await run('browser Back keeps a dirty draft until discard is accepted', async ({ page, docs, logs, controls }) => {
    await page.goto(base)
    await page.waitForLoadState('networkidle')
    await page.locator('.document-title').first().click()
    await page.locator('.drawer-edit').click()
    const draft = '# Unsaved work'
    await page.locator('#document-editor').fill(draft)
    const documentUrl = page.url()
    await back(page)
    assert.equal(logs.confirmDialogs.length, 1)
    await page.waitForURL(documentUrl)
    assert.equal(await page.locator('#document-editor').inputValue(), draft)
    controls.acceptDiscard = true
    await back(page)
    await page.waitForURL(base + '/')
    await page.locator('dialog[open]').waitFor({ state: 'hidden' })
    assert.equal(logs.confirmDialogs.length, 2)
    await page.locator('.document-title').first().click()
    await page.locator('.drawer-edit').click()
    assert.equal(await page.locator('#document-editor').inputValue(), docs[0].content)
    assert.equal(logs.writes.length, 0)
  })

  await run('explicit close asks once and preserves a rejected draft', async ({ page, logs, controls }) => {
    await page.goto(base + '/?document=1')
    await page.locator('.drawer-edit').click()
    await page.locator('#document-editor').fill('# Unsaved close')
    await page.locator('.drawer-header .icon-button').click()
    assert.equal(await page.locator('#document-editor').inputValue(), '# Unsaved close')
    assert.equal(logs.confirmDialogs.length, 1)
    controls.acceptDiscard = true
    await page.locator('.drawer-header .icon-button').click()
    await page.locator('dialog[open]').waitFor({ state: 'hidden' })
    assert.equal(logs.confirmDialogs.length, 2)
  })

  await run('saving holds document identity and invalidates its answer', async ({ page, docs, logs, controls }) => {
    await page.goto(base + '/ask')
    await page.waitForLoadState('networkidle')
    await page.locator('#knowledge-question').fill('Question about document 1')
    await page.locator('.send-button').click()
    await page.locator('.answer-paper').waitFor()
    await page.locator('#citation-1 .source-open').click()
    await page.locator('.drawer-edit').click()
    await page.locator('#document-editor').fill('# Saved update for document 1')
    const documentUrl = page.url()
    controls.holdWrite = true
    await page.locator('.drawer-footer .button-primary').click()
    await controls.mutationStarted
    await back(page)
    await page.waitForURL(documentUrl)
    assert.equal(logs.confirmDialogs.length, 0)
    assert.equal(await page.locator('#document-editor').inputValue(), '# Saved update for document 1')
    assert.equal(await page.locator('.drawer-header .icon-button').isDisabled(), true)
    controls.mutation()
    await page.waitForFunction(() => !document.querySelector('.drawer-header .icon-button').disabled)
    await page.locator('.drawer-notice.notice-success').waitFor()
    assert.equal(docs[0].content, '# Saved update for document 1')
    assert.equal(logs.writes[0].id, 1)
    await page.locator('.drawer-header .icon-button').click()
    await page.locator('.answer-notice').waitFor()
    await page.locator('.app-nav a[href="/"]').click()
    await page.locator('.document-title').nth(1).click()
    await page.locator('#document-heading').filter({ hasText: docs[1].title }).waitFor()
    assert.equal(await page.locator('.drawer-notice.notice-success').count(), 0)
    assert.equal(await page.locator('#document-preview').innerText(), 'Original 2')
  })

  await run('successful deletion can close through the navigation guard', async ({ page, docs }) => {
    await page.goto(base + '/?document=1')
    await page.locator('.delete-button').click()
    await page.locator('.button-danger').click()
    await page.locator('dialog[open]').waitFor({ state: 'hidden' })
    assert.deepEqual(docs.map(doc => doc.id), [2])
    assert.equal(new URL(page.url()).searchParams.has('document'), false)
  })

  await run('a stale READY refresh cannot overwrite a newer mutation conflict', async ({ page, runtime, controls }) => {
    await page.goto(base + '/ask')
    await page.waitForLoadState('networkidle')
    await page.locator('#knowledge-question').fill('Question during mutation')
    controls.holdHealth = true
    const oldRuntime = page.waitForResponse(response => response.url() === base + '/api/runtime')
    await page.locator('.connection-state').click()
    assert.equal((await (await oldRuntime).json()).state, 'READY')
    controls.onQuestion = route => {
      runtime.state = 'MUTATING'
      return route.fulfill({ status: 503, json: { error: 'Fixture mutation in progress', state: 'MUTATING' } })
    }
    await page.locator('.send-button').click()
    await page.locator('.question-error').waitFor()
    assert.equal(controls.healthWaiters.length, 1)
    assert.equal(await page.locator('.send-button').isDisabled(), true)
    const freshRuntime = page.waitForResponse(response => response.url() === base + '/api/runtime')
    controls.healthWaiters[0]()
    assert.equal((await (await freshRuntime).json()).state, 'MUTATING')
    assert.equal(await page.locator('.send-button').isDisabled(), true)
    assert.equal(controls.healthWaiters.length, 2)
    controls.holdHealth = false
    controls.healthWaiters[1]()
    await page.waitForFunction(() => !document.querySelector('.connection-state').disabled)
    assert.equal(await page.locator('.send-button').isDisabled(), true)
    assert.equal(await page.locator('.app-notices .notice-warm').count(), 1)
  })

  await run('successful recovery refreshes after a pending old snapshot', async ({ page, runtime, controls }) => {
    runtime.state = 'RECOVERY_REQUIRED'
    await page.goto(base + '/ask')
    await page.waitForLoadState('networkidle')
    await page.locator('#knowledge-question').fill('Question after recovery')
    controls.holdHealth = true
    const oldRuntime = page.waitForResponse(response => response.url() === base + '/api/runtime')
    await page.locator('.connection-state').click()
    assert.equal((await (await oldRuntime).json()).state, 'RECOVERY_REQUIRED')
    const readyResponse = page.waitForResponse(response => response.url() === base + '/api/admin/ready')
    await page.getByRole('button', { name: '确认就绪', exact: true }).click()
    await readyResponse
    assert.equal(controls.readyCalls, 1)
    assert.equal(controls.healthWaiters.length, 1)
    controls.holdHealth = false
    controls.healthWaiters[0]()
    await page.waitForFunction(() => !document.querySelector('.connection-state').disabled)
    await page.waitForFunction(() => !document.querySelector('.send-button').disabled)
    assert.equal(await page.locator('.app-notices .notice-warm').count(), 0)
  })

  await run('successful save requests a fresh state after an old poll', async ({ page, runtime, controls }) => {
    await page.goto(base + '/?document=1')
    await page.waitForLoadState('networkidle')
    // The modal makes the header inert; invoke the same refresh action directly.
    controls.holdHealth = true
    const oldRuntime = page.waitForResponse(response => response.url() === base + '/api/runtime')
    await page.evaluate(async () => {
      const { useGateStore } = await import('/src/shared/gate.ts')
      void useGateStore().refresh()
    })
    assert.equal((await (await oldRuntime).json()).state, 'READY')
    await page.locator('.drawer-edit').click()
    await page.locator('#document-editor').fill('# Updated while polling')
    controls.afterWrite = () => { runtime.state = 'MUTATING' }
    const saved = page.waitForResponse(response => response.request().method() === 'PUT')
    await page.locator('.drawer-footer .button-primary').click()
    await saved
    await page.locator('.drawer-notice.notice-success').waitFor()
    assert.equal(controls.healthWaiters.length, 1)
    controls.holdHealth = false
    controls.healthWaiters[0]()
    await page.waitForFunction(() => !document.querySelector('.drawer-header .icon-button').disabled)
    assert.equal(await page.locator('.app-notices .notice-warm').count(), 1)
    assert.equal(await page.locator('.drawer-edit').isDisabled(), true)
  })


  await run('upload completion invalidates an older runtime snapshot', async ({ page, runtime, controls }) => {
    await page.goto(base)
    await page.waitForLoadState('networkidle')
    controls.holdHealth = true
    const oldRuntime = page.waitForResponse(response => response.url() === base + '/api/runtime')
    await page.locator('.connection-state').click()
    assert.equal((await (await oldRuntime).json()).state, 'READY')
    controls.onUpload = () => { runtime.state = 'MUTATING' }
    await page.evaluate(async () => {
      const { useDocumentStore } = await import('/src/features/documents/store.ts')
      await useDocumentStore().upload(new File(['# Uploaded'], 'upload.md', { type: 'text/markdown' }))
    })
    assert.equal(controls.healthWaiters.length, 1)
    controls.holdHealth = false
    controls.healthWaiters[0]()
    await page.waitForFunction(() => !document.querySelector('.connection-state').disabled)
    assert.equal(await page.locator('.app-notices .notice-warm').count(), 1)
  })

  await run('a stale refresh failure cannot hide a newer operation conflict', async ({ page, runtime, controls }) => {
    await page.goto(base + '/ask')
    await page.waitForLoadState('networkidle')
    await page.locator('#knowledge-question').fill('Question during mutation')
    controls.holdHealth = true
    const oldRuntime = page.waitForResponse(response => response.url() === base + '/api/runtime')
    await page.locator('.connection-state').click()
    await oldRuntime
    controls.onQuestion = route => {
      runtime.state = 'MUTATING'
      return route.fulfill({ status: 503, json: { error: 'Fixture mutation in progress', state: 'MUTATING' } })
    }
    await page.locator('.send-button').click()
    await page.locator('.question-error').waitFor()
    const freshRuntime = page.waitForResponse(response => response.url() === base + '/api/runtime')
    controls.failHealth = true
    controls.healthWaiters[0]()
    await freshRuntime
    assert.equal(await page.locator('.app-notices .notice-error').count(), 0)
    assert.equal(await page.locator('.send-button').isDisabled(), true)
    controls.holdHealth = false
    controls.healthWaiters[1]()
    await page.waitForFunction(() => !document.querySelector('.connection-state').disabled)
    assert.equal(await page.locator('.app-notices .notice-warm').count(), 1)
  })


  await run('retrieval adoption stays distinct from citations in current and saved answers', async ({ page, answer, docs }) => {
    docs.push({ ...docs[0], id: 3, title: 'Adopted but not cited', content: '# Additional evidence' })
    answer.answer = 'Supported answer [2]'
    answer.sources.push({ ...answer.sources[0], chunk_id: 30, document_id: 3, title: docs[2].title, text: docs[2].content })
    answer.trace[0].relevant = [2, 3]
    answer.trace[0].retrieved = [
      { chunk_id: 20, document_id: 2, rank: 1, score: 0.9 },
      { chunk_id: 10, document_id: 1, rank: 2, score: 0.8 },
      { chunk_id: 30, document_id: 3, rank: 3, score: 0.7 },
    ]
    const artifacts = new URL('../.verification/u7/', import.meta.url)
    await mkdir(artifacts, { recursive: true })
    await page.goto(base + '/ask')
    await page.waitForLoadState('networkidle')
    await page.locator('#knowledge-question').fill('Show the evidence selection')
    await page.locator('.send-button').click()
    await page.locator('[data-history-id="1"]').waitFor()
    for (const mode of ['current', 'history']) {
      if (mode === 'history') {
        await page.reload()
        await page.locator('[data-history-id="1"] .history-select').click()
        await page.locator('.history-answer-context').waitFor()
      }
      await page.locator('.trace-details > summary').click()
      assert.deepEqual(await page.locator('.retrieval-label').allTextContents(), ['未进生成', '采用 · 正文引用 2', '采用'])
      assert.equal(await page.locator('.trace-adoption-unavailable').count(), 0)
      assert.equal(await page.locator('.source-card').count(), 1, 'only actual citations have source cards')
      await page.locator('.answer-body .inline-citation').click()
      await page.waitForFunction(() => document.activeElement?.id === 'citation-2')
      for (const width of [1440, 390]) {
        await page.setViewportSize({ width, height: 1000 })
        await page.locator('.trace-details').scrollIntoViewIfNeeded()
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true, `${mode}: no overflow at ${width}px`)
        assert.equal(await page.locator('.retrieval-label').evaluateAll(labels => labels.every(label => {
          const row = label.closest('li').getBoundingClientRect(), bounds = label.getBoundingClientRect()
          return bounds.left >= row.left - 1 && bounds.right <= row.right + 1
        })), true, `${mode}: labels fit at ${width}px`)
        await page.screenshot({ path: fileURLToPath(new URL(`${mode}-${width}.png`, artifacts)), fullPage: true })
      }
    }
    await page.locator('.retrieval-snapshot > summary').last().click()
    assert.equal(await page.locator('.retrieval-snapshot .source-snapshot').last().textContent(), docs[2].content)
  })

  await run('legacy history explains missing adoption without inferring it from saved sources', async ({ page, answer, seedHistory }) => {
    answer.trace[0].retrieved.push({ chunk_id: 20, document_id: 2, rank: 2, score: 0.7 })
    seedHistory(1, 'Legacy answer')
    await page.goto(base + '/ask')
    await page.locator('[data-history-id="1"] .history-select').click()
    await page.locator('.history-answer-context').waitFor()
    await page.locator('.trace-details > summary').click()
    assert.deepEqual(await page.locator('.retrieval-label').allTextContents(), ['正文引用 1', '检索结果'])
    assert.equal(await page.locator('.trace-adoption-unavailable').innerText(), '此记录未保存片段采用情况。')
  })

  await run('refused answers show explicit exclusions and an empty retrieval stays readable', async ({ page, answer, seedHistory }) => {
    answer.answer = 'No supporting evidence'
    answer.status = 'REFUSED'
    answer.sources = []
    answer.trace[0].decision = 'NONE'
    answer.trace[0].relevant = []
    seedHistory(1, 'Rejected candidates')
    answer.trace[0].retrieved = []
    seedHistory(2, 'No candidates')
    await page.goto(base + '/ask')
    await page.locator('[data-history-id="1"] .history-select').click()
    await page.locator('.history-answer-context').waitFor()
    await page.locator('.trace-details > summary').click()
    assert.deepEqual(await page.locator('.retrieval-label').allTextContents(), ['未进生成'])
    assert.equal(await page.locator('.trace-adoption-unavailable').count(), 0)
    await page.locator('[data-history-id="2"] .history-select').click()
    await page.locator('.answer-question h2').filter({ hasText: 'No candidates' }).waitFor()
    await page.locator('.trace-details > summary').evaluate(element => { element.parentElement.open = true })
    assert.equal(await page.locator('.retrieval-label').count(), 0)
    assert.equal(await page.locator('.trace-no-results').innerText(), '本轮没有检索到相关片段。')
  })

  await run('saved history survives reload and shows the full deleted-source snapshot', async ({ page, answer, docs, runtime, logs }) => {
    const snapshot = Array.from({ length: 14 }, (_, index) => 'Saved source line ' + (index + 1)).join('\n')
    answer.sources[0].text = snapshot + '\n<img src=x onerror=alert(1)>'
    answer.answer = 'Saved answer [1]\n\n<script>window.historyInjected = true</script>'
    await page.goto(base + '/ask')
    await page.waitForLoadState('networkidle')
    assert.equal(await page.locator('.question-history').count(), 1, 'history panel is present')
    await page.locator('#knowledge-question').fill('Remember this answer')
    await page.locator('.send-button').click()
    await page.locator('[data-history-id="1"]').waitFor()
    docs.splice(0, 1)
    runtime.llm.model = 'changed-after-save'
    await page.evaluate(() => localStorage.clear())
    await page.reload()
    await page.locator('[data-history-id="1"] .history-select').click()
    await page.locator('.answer-question h2').filter({ hasText: 'Remember this answer' }).waitFor()
    assert.equal(await page.locator('.source-card .source-snapshot').textContent(), answer.sources[0].text)
    assert.equal(await page.locator('.source-card .source-snapshot').evaluate(element => element.clientHeight >= element.scrollHeight), true)
    assert.equal(await page.locator('.source-card .source-open, .source-card button.source-title').count(), 0)
    assert.match(await page.locator('.answer-model').innerText(), /openai.*fixture-model/)
    assert.match(await page.locator('.answer-saved-at').innerText(), /2026-09-15 12:00:00/)
    assert.match(await page.locator('.answer-bottomline').innerText(), /2\.4/)
    await page.locator('.trace-details > summary').click()
    await page.locator('.retrieval-snapshot > summary').click()
    assert.equal(await page.locator('.retrieval-snapshot .source-snapshot').textContent(), answer.sources[0].text)
    assert.equal(await page.locator('.answer-body script, .source-snapshot img').count(), 0)
    assert.equal(await page.evaluate(() => window.historyInjected), undefined)
    assert.equal(new URL(page.url()).searchParams.has('document'), false)
    assert.deepEqual(logs.documentReads, [])
    assert.equal(logs.questions.length, 1)
    assert.ok(logs.historyReads.length >= 3)
  })

  await run('history pagination returns to the preceding page after deleting its last record', async ({ page, history, seedHistory, logs }) => {
    for (let id = 1; id <= 21; id++) seedHistory(id, 'Saved question ' + id)
    await page.goto(base + '/ask')
    await page.waitForLoadState('networkidle')
    assert.equal(await page.locator('.question-history').count(), 1, 'history panel is present')
    assert.equal(await page.locator('.history-select').count(), 20)
    assert.match(await page.locator('.history-select').first().innerText(), /Saved question 21/)
    await page.getByRole('button', { name: '\u4e0b\u4e00\u9875', exact: true }).click()
    await page.locator('[data-history-id="1"]').waitFor()
    assert.equal(await page.locator('.history-select').count(), 1)
    await page.locator('[data-history-id="1"] .history-select').click()
    await page.locator('.answer-question h2').filter({ hasText: 'Saved question 1' }).waitFor()
    await page.locator('[data-history-id="1"] .history-delete').click()
    await page.locator('.history-confirm-delete').click()
    await page.locator('[data-history-id="21"]').waitFor()
    assert.equal(await page.locator('.history-select').count(), 20)
    assert.equal(await page.locator('.answer-paper').count(), 0)
    assert.equal(await page.getByRole('button', { name: '\u4e0a\u4e00\u9875', exact: true }).isDisabled(), true)
    assert.equal(history.length, 20)
    assert.deepEqual(logs.historyDeletes, [1])
    assert.deepEqual(logs.knowledgeWrites, [])
    assert.deepEqual(logs.questions, [])
    assert.ok(logs.historyReads.includes('?page=1&size=20'))
  })

  await run('history remains readable and deletable while retrieval is unavailable', async ({ page, seedHistory, runtime, logs }) => {
    seedHistory(1, 'Saved with sources')
    seedHistory(2, 'Saved refusal', { status: 'REFUSED', answer: 'No evidence was sufficient.', sources: [], trace: [{ round_index: 1, query: 'Unrelated question', decision: 'NONE', retrieved: [{ document_id: 99, chunk_id: 990, rank: 1, score: 0.1 }] }] })
    runtime.rag_available = false
    runtime.llm.configured = false
    await page.goto(base + '/ask')
    await page.waitForLoadState('networkidle')
    assert.equal(await page.locator('.question-history').count(), 1, 'history panel is present')
    await page.locator('[data-history-id="2"] .history-select').click()
    await page.locator('.answer-question h2').filter({ hasText: 'Saved refusal' }).waitFor()
    assert.equal(await page.locator('.history-reask').isDisabled(), true)
    await page.locator('.trace-details > summary').click()
    assert.match(await page.locator('.retrieval-unavailable').innerText(), /990/)
    assert.equal(await page.locator('.retrieval-list button, .retrieval-list a, .retrieval-snapshot').count(), 0)
    await page.locator('[data-history-id="1"] .history-select').click()
    await page.locator('.source-card .source-snapshot').waitFor()
    await page.locator('[data-history-id="2"] .history-delete').click()
    await page.locator('.history-confirm-delete').click()
    await page.locator('[data-history-id="2"]').waitFor({ state: 'hidden' })
    assert.equal(await page.locator('.answer-question h2').innerText(), 'Saved with sources')
    assert.deepEqual(logs.questions, [])
    assert.deepEqual(logs.documentReads, [])
    assert.deepEqual(logs.knowledgeWrites, [])
    assert.deepEqual(logs.historyDeletes, [2])
  })

  await run('reasking saves another record with response metadata and only the original question', async ({ page, seedHistory, answer, history, runtime, logs }) => {
    seedHistory(1, 'Ask this again', { model: { provider: 'ollama', model: 'old-saved-model' }, elapsed_ms: 10000 })
    runtime.llm.model = 'cached-runtime-model'
    answer.model = { provider: 'openai', model: 'current-answer-model' }
    answer.elapsed_ms = 3700
    await page.goto(base + '/ask')
    await page.waitForLoadState('networkidle')
    assert.equal(await page.locator('.question-history').count(), 1, 'history panel is present')
    await page.locator('[data-history-id="1"] .history-select').click()
    await page.locator('.history-reask').click()
    await page.locator('[data-history-id="2"]').waitFor()
    assert.equal(await page.locator('.answer-question h2').innerText(), 'Ask this again')
    assert.match(await page.locator('.answer-model').innerText(), /openai.*current-answer-model/)
    assert.match(await page.locator('.answer-bottomline').innerText(), /3\.7/)
    assert.deepEqual(logs.questions, [{ question: 'Ask this again' }])
    assert.equal(history.length, 2)
    assert.equal(history[0].model.model, 'old-saved-model')
    assert.deepEqual(logs.knowledgeWrites, [])
  })

  await run('a delayed history detail cannot replace the later selection', async ({ page, seedHistory, history, controls }) => {
    seedHistory(1, 'Slow first selection')
    seedHistory(2, 'Latest selection')
    let release
    const held = new Promise(resolve => { release = resolve })
    controls.pendingReleases.push(release)
    controls.onHistoryDetail = async (route, id) => {
      if (id === 1) await held
      return route.fulfill({ json: history.find(record => record.id === id) })
    }
    await page.goto(base + '/ask')
    await page.waitForLoadState('networkidle')
    assert.equal(await page.locator('.question-history').count(), 1, 'history panel is present')
    const slowRequest = page.waitForRequest(base + '/api/question-history/1')
    await page.locator('[data-history-id="1"] .history-select').click()
    await slowRequest
    await page.locator('.history-detail-loading').waitFor()
    assert.equal(await page.locator('[data-history-id="1"] .history-select').getAttribute('aria-current'), 'true')
    await page.locator('[data-history-id="2"] .history-select').click()
    await page.locator('.answer-question h2').filter({ hasText: 'Latest selection' }).waitFor()
    await page.locator('.app-nav a[href="/ask"]').focus()
    const oldResponse = page.waitForResponse(base + '/api/question-history/1')
    release()
    await (await oldResponse).finished()
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))))
    assert.equal(await page.locator('.answer-question h2').innerText(), 'Latest selection')
    assert.equal(await page.locator('[data-history-id="2"] .history-select').getAttribute('aria-current'), 'true')
    assert.equal(await page.evaluate(() => document.activeElement === document.querySelector('.app-nav a[href="/ask"]')), true, 'a stale detail must not reclaim focus')
  })

  await run('a completed question cannot replace history selected during generation', async ({ page, seedHistory, completeQuestion, controls }) => {
    seedHistory(1, 'History kept on screen')
    let release
    const held = new Promise(resolve => { release = resolve })
    controls.pendingReleases.push(release)
    controls.onQuestion = async route => { await held; return completeQuestion(route) }
    await page.goto(base + '/ask')
    await page.waitForLoadState('networkidle')
    assert.equal(await page.locator('.question-history').count(), 1, 'history panel is present')
    await page.locator('#knowledge-question').fill('New question in progress')
    const pending = page.waitForRequest(base + '/api/questions/stream')
    await page.locator('.send-button').click()
    await pending
    await page.locator('[data-history-id="1"] .history-select').click()
    await page.locator('.answer-question h2').filter({ hasText: 'History kept on screen' }).waitFor()
    assert.match(await page.locator('.answer-loading').innerText(), /New question in progress/)
    await page.locator('.app-nav a[href="/ask"]').focus()
    release()
    await page.locator('[data-history-id="2"]').waitFor()
    assert.equal(await page.locator('.answer-question h2').innerText(), 'History kept on screen')
    assert.equal(await page.locator('[data-history-id="1"] .history-select').getAttribute('aria-current'), 'true')
    assert.equal(await page.evaluate(() => document.activeElement === document.querySelector('.app-nav a[href="/ask"]')), true, 'a background answer must not reclaim focus')
    await page.locator('.question-saved-notice button').click()
    await page.locator('.answer-question h2').filter({ hasText: 'New question in progress' }).waitFor()
  })

  await run('deleting a record invalidates its pending detail response', async ({ page, seedHistory, controls, logs }) => {
    const record = seedHistory(1, 'Removed while loading')
    let release
    const held = new Promise(resolve => { release = resolve })
    controls.pendingReleases.push(release)
    controls.onHistoryDetail = async route => { await held; return route.fulfill({ json: record }) }
    await page.goto(base + '/ask')
    await page.waitForLoadState('networkidle')
    assert.equal(await page.locator('.question-history').count(), 1, 'history panel is present')
    const pending = page.waitForRequest(base + '/api/question-history/1')
    await page.locator('[data-history-id="1"] .history-select').click()
    await pending
    await page.locator('[data-history-id="1"] .history-delete').click()
    await page.locator('.history-confirm-delete').click()
    await page.locator('[data-history-id="1"]').waitFor({ state: 'hidden' })
    const oldResponse = page.waitForResponse(response => response.url() === base + '/api/question-history/1' && response.request().method() === 'GET')
    release()
    await (await oldResponse).finished()
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))))
    assert.equal(await page.locator('.answer-paper, .history-detail-loading').count(), 0)
    assert.deepEqual(logs.historyDeletes, [1])
    assert.deepEqual(logs.knowledgeWrites, [])
  })

  await run('a history storage failure does not display or create a completed answer', async ({ page, seedHistory, controls, history, logs }) => {
    seedHistory(1, 'Earlier saved answer')
    controls.onQuestion = route => route.fulfill({ status: 500, json: { error: 'History storage failed' } })
    await page.goto(base + '/ask')
    await page.waitForLoadState('networkidle')
    assert.equal(await page.locator('.question-history').count(), 1, 'history panel is present')
    await page.locator('#knowledge-question').fill('Unsaved question')
    await page.locator('.send-button').click()
    await page.locator('.question-error').waitFor()
    assert.match(await page.locator('.question-error').innerText(), /History storage failed/)
    assert.equal(await page.locator('.answer-paper, .answer-loading, .question-saved-notice').count(), 0)
    assert.equal(await page.locator('.history-select').count(), 1)
    assert.equal(history.length, 1)
    assert.equal(logs.questions.length, 1)
    await page.locator('[data-history-id="1"] .history-select').click()
    await page.locator('.answer-question h2').filter({ hasText: 'Earlier saved answer' }).waitFor()
  })

  await run('an older list response cannot erase a newly saved record', async ({ page, controls, completeQuestion, historyPage }) => {
    let release
    const held = new Promise(resolve => { release = resolve })
    controls.pendingReleases.push(release)
    let first = true
    controls.onHistoryList = async (route, url) => {
      if (first) {
        first = false
        await held
        return route.fulfill({ json: { total: 0, items: [] } })
      }
      return route.fulfill({ json: historyPage(url) })
    }
    controls.onQuestion = completeQuestion
    await page.goto(base + '/ask')
    await page.locator('.question-composer').waitFor()
    assert.equal(await page.locator('.question-history').count(), 1, 'history panel is present')
    await page.locator('#knowledge-question').fill('Saved during old history load')
    await page.locator('.send-button').click()
    await page.locator('[data-history-id="1"]').waitFor()
    const oldResponse = page.waitForResponse(base + '/api/question-history?page=0&size=20')
    release()
    await (await oldResponse).finished()
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))))
    assert.equal(await page.locator('.history-select').count(), 1)
    assert.match(await page.locator('.history-select').innerText(), /Saved during old history load/)
  })


  await run('confirmed deletion stays removed when refresh fails and an older list arrives', async ({ page, seedHistory, historyPage, controls, logs }) => {
    for (let id = 1; id <= 4; id++) seedHistory(id, 'Deletion review ' + id)
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await page.goto(base + '/ask')
    await page.waitForLoadState('networkidle')
    for (const [index, arrival] of ['before', 'after'].entries()) {
      const id = index + 1
      const selector = '[data-history-id="' + id + '"]'
      await page.locator(selector + ' .history-select').click()
      await page.locator('.answer-question h2').filter({ hasText: 'Deletion review ' + id }).waitFor()
      let releaseList, releaseDelete, deleteStarted
      const oldList = new Promise(resolve => { releaseList = resolve })
      const deleteAck = new Promise(resolve => { releaseDelete = resolve })
      const deletion = new Promise(resolve => { deleteStarted = resolve })
      controls.pendingReleases.push(releaseList, releaseDelete)
      let firstList = true
      controls.onHistoryList = async (route, url) => {
        if (firstList) {
          firstList = false
          const oldPage = structuredClone(historyPage(url))
          await oldList
          return route.fulfill({ json: oldPage })
        }
        return route.fulfill({ status: 500, json: { error: 'History refresh failed' } })
      }
      controls.onHistoryDelete = async () => { deleteStarted(); await deleteAck }
      const listed = page.waitForRequest(base + '/api/question-history?page=0&size=20')
      await page.locator('.history-refresh').click()
      const oldRequest = await listed
      await page.locator(selector + ' .history-delete').click()
      await page.locator('.history-confirm-delete').click()
      await deletion
      assert.equal(await page.locator(selector).count(), 1, 'keep the row until DELETE is acknowledged')
      if (arrival === 'before') {
        releaseList()
        await (await oldRequest.response()).finished()
        await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))))
        assert.equal(await page.locator(selector).count(), 1, 'an unacknowledged deletion is not optimistic')
      }
      releaseDelete()
      await page.locator('.history-error').filter({ hasText: 'History refresh failed' }).waitFor()
      assert.equal(await page.locator(selector).count(), 0, 'a confirmed deletion remains removed when list refresh fails')
      assert.equal(await page.locator('.history-heading .count-badge').innerText(), String(4 - id))
      if (arrival === 'after') {
        releaseList()
        await (await oldRequest.response()).finished()
        await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))))
        assert.equal(await page.locator(selector).count(), 0, 'a stale list cannot restore the deleted record')
        assert.equal(await page.locator('.history-heading .count-badge').innerText(), String(4 - id))
      }
      assert.equal(await page.locator('.answer-paper').count(), 0)
    }
    for (let id = 5; id <= 23; id++) seedHistory(id, 'Deletion review ' + id)
    controls.onHistoryDelete = null
    controls.onHistoryList = null
    await page.locator('.history-refresh').click()
    await page.locator('[data-history-id="23"]').waitFor()
    for (const [index, commit] of ['before', 'after'].entries()) {
      const id = index + 3
      const selector = '[data-history-id="' + id + '"]'
      const question = 'Saved while deletion waits ' + commit + ' commit'
      await page.getByRole('button', { name: '\u4e0b\u4e00\u9875', exact: true }).click()
      await page.locator(selector).waitFor()
      assert.equal(await page.locator('.history-select').count(), 1)
      let releaseDelete, deleteStarted
      const deleteAck = new Promise(resolve => { releaseDelete = resolve })
      const deletion = new Promise(resolve => { deleteStarted = resolve })
      controls.pendingReleases.push(releaseDelete)
      const holdDeletion = async () => { deleteStarted(); await deleteAck }
      controls.beforeHistoryDelete = commit === 'before' ? holdDeletion : null
      controls.onHistoryDelete = commit === 'after' ? holdDeletion : null
      await page.locator(selector + ' .history-delete').click()
      await page.locator('.history-confirm-delete').click()
      await deletion
      const listResponse = page.waitForResponse(base + '/api/question-history?page=0&size=20')
      await page.locator('#knowledge-question').fill(question)
      await page.locator('.send-button').click()
      await page.locator('.answer-question h2').filter({ hasText: question }).waitFor()
      const response = await listResponse
      const concurrentPage = await response.json()
      await response.finished()
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))))
      assert.equal(concurrentPage.items.some(item => item.id === id), false, 'page one omits the deleted ID on either side of the server commit')
      assert.equal(concurrentPage.total, commit === 'before' ? 22 : 21)
      assert.equal(await page.locator(selector).count(), 1, 'keep the confirmed page while DELETE is pending')
      assert.equal(await page.locator('.history-heading .count-badge').innerText(), '21', 'do not adopt a concurrent total before DELETE is acknowledged')
      assert.match(await page.locator('.history-pagination').innerText(), /2 \/ 2/)
      controls.onHistoryList = route => route.fulfill({ status: 500, json: { error: 'History refresh failed' } })
      releaseDelete()
      await page.locator('.history-error').filter({ hasText: 'History refresh failed' }).waitFor()
      assert.equal(await page.locator(selector).count(), 0)
      assert.equal(await page.locator('.history-select').count(), 0)
      assert.equal(await page.locator('.history-heading .count-badge').innerText(), '20', 'remove only the confirmed deletion from the retained snapshot')
      assert.match(await page.locator('.history-pagination').innerText(), /1 \/ 1/)
      assert.equal(await page.getByRole('button', { name: '\u4e0a\u4e00\u9875', exact: true }).isDisabled(), true)
      assert.equal(await page.locator('.answer-question h2').innerText(), question, 'deleting an earlier record does not clear the new answer')
      controls.beforeHistoryDelete = null
      controls.onHistoryDelete = null
      controls.onHistoryList = null
      await page.locator('.history-refresh').click()
      await page.locator('[data-history-id="' + (index + 24) + '"]').waitFor()
      assert.equal(await page.locator('.history-select').count(), 20)
      assert.equal(await page.locator('.history-heading .count-badge').innerText(), '21', 'a successful retry restores the authoritative total')
    }
    assert.deepEqual(logs.historyDeletes, [1, 2, 3, 4])
    assert.deepEqual(logs.knowledgeWrites, [])
    assert.deepEqual(logs.questions, [
      { question: 'Saved while deletion waits before commit' },
      { question: 'Saved while deletion waits after commit' },
    ])
  })

  await run('selecting history below a long answer brings its question into the narrow viewport', async ({ page, seedHistory, answer }) => {
    const text = Array.from({ length: 70 }, (_, index) => 'Full saved source line ' + (index + 1)).join('\n')
    for (const id of [1, 2]) seedHistory(id, 'Long saved answer ' + id, {
      answer: Array.from({ length: 20 }, () => 'A paragraph in the saved explanation.').join('\n\n') + '\n\nSaved evidence [1]',
      sources: [{ ...answer.sources[0], text }],
    })
    await page.setViewportSize({ width: 390, height: 844 })
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await page.goto(base + '/ask')
    await page.waitForLoadState('networkidle')
    await page.locator('[data-history-id="1"] .history-select').click()
    await page.locator('.answer-question h2').filter({ hasText: 'Long saved answer 1' }).waitFor()
    const next = page.locator('[data-history-id="2"] .history-select')
    await next.scrollIntoViewIfNeeded()
    assert.equal(await page.locator('.answer-question h2').evaluate(element => element.getBoundingClientRect().bottom < 0), true, 'the previous heading starts above the viewport')
    await next.click()
    const heading = page.locator('.answer-question h2').filter({ hasText: 'Long saved answer 2' })
    await heading.waitFor()
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))))
    const visible = await heading.evaluate(element => {
      const rect = element.getBoundingClientRect()
      return rect.top >= 0 && rect.bottom <= window.innerHeight
    })
    assert.equal(visible, true, 'the selected history question should be visible without manual scrolling')
  })

  await run('stream preview stays incomplete on EOF and does not create local history', async ({ page, controls, history }) => {
    controls.onQuestion = route => route.fulfill({ contentType: 'text/event-stream', body: 'event: delta\ndata: "Preview text [1]"\n\n' })
    await page.goto(base + '/ask')
    await page.waitForLoadState('networkidle')
    await page.locator('#knowledge-question').fill('Stream question')
    await page.locator('.send-button').click()
    await page.locator('.question-error').waitFor()
    assert.equal(await page.locator('.stream-preview-text').textContent(), 'Preview text [1]')
    assert.equal(history.length, 0)
    assert.equal(await page.locator('.answer-question').count(), 0)
    assert.equal(await page.locator('.stream-preview .eyebrow').textContent(), '回答未完成，以下内容仅供预览')
  })

  await run('browser displays a live delta before completion and ignores it after history selection', async ({ page, seedHistory, answer }) => {
    seedHistory(9, 'Saved historical question')
    await page.addInitScript(() => {
      const original = window.fetch
      window.fetch = async (input, init) => {
        if (input !== '/api/questions/stream') return original(input, init)
        return new Response(new ReadableStream({ start(controller) { window.streamTestController = controller } }), {
          headers: { 'Content-Type': 'text/event-stream' },
        })
      }
    })
    await page.goto(base + '/ask')
    await page.waitForLoadState('networkidle')
    await page.locator('#knowledge-question').fill('Live question')
    await page.locator('.send-button').click()
    await page.waitForFunction(() => Boolean(window.streamTestController))
    await page.evaluate(() => window.streamTestController.enqueue(new TextEncoder().encode('event: delta\ndata: "Visible before done"\n\n')))
    await page.locator('.stream-preview-text').filter({ hasText: 'Visible before done' }).waitFor()
    assert.equal(await page.locator('.answer-question').count(), 0)
    await page.locator('[data-history-id="9"] .history-select').click()
    await page.locator('.answer-question h2').filter({ hasText: 'Saved historical question' }).waitFor()
    await page.evaluate(result => {
      window.streamTestController.enqueue(new TextEncoder().encode('event: delta\ndata: "Late preview"\n\n'))
      window.streamTestController.enqueue(new TextEncoder().encode(`event: done\ndata: ${JSON.stringify(result)}\n\n`))
    }, { ...answer, history_id: 10 })
    await page.locator('.question-saved-notice').waitFor()
    assert.equal(await page.locator('.stream-preview').count(), 0)
    assert.equal(await page.locator('.answer-question h2').textContent(), 'Saved historical question')
  })

  await run('a background stream failure identifies its question without replacing selected history', async ({ page, seedHistory }) => {
    seedHistory(9, 'Historical question')
    await page.addInitScript(() => {
      const original = window.fetch
      window.fetch = async (input, init) => {
        if (input !== '/api/questions/stream') return original(input, init)
        return new Response(new ReadableStream({ start(controller) { window.streamTestController = controller } }), {
          headers: { 'Content-Type': 'text/event-stream' },
        })
      }
    })
    await page.goto(base + '/ask')
    await page.waitForLoadState('networkidle')
    await page.locator('#knowledge-question').fill('Background question')
    await page.locator('.send-button').click()
    await page.waitForFunction(() => Boolean(window.streamTestController))
    await page.locator('[data-history-id="9"] .history-select').click()
    await page.locator('.answer-question h2').filter({ hasText: 'Historical question' }).waitFor()
    await page.evaluate(() => window.streamTestController.enqueue(new TextEncoder().encode('event: error\ndata: {"error":"Generation failed"}\n\n')))
    await page.locator('.question-error').filter({ hasText: '另一条问题「Background question」的回答未完成' }).waitFor()
    assert.equal(await page.locator('.answer-question h2').textContent(), 'Historical question')
  })

  console.log('REGRESSIONS_COMPLETE', JSON.stringify(results))
  if (results.some(result => !result.passed)) process.exitCode = 1
} finally {
  await browser?.close()
  await server.close()
}
