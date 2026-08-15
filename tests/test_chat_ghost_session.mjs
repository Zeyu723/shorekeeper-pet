import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import vm from 'node:vm'
import { fileURLToPath } from 'node:url'

// Regression for the "ghost session" bug (2026-08-16):
// When the desktop sits on a fresh New Session welcome draft there is NO
// activeSessionId, so the bridge created a backend session via
// session.create — but never told the UI. The session existed in state.db
// yet never appeared in the sidebar/main view ("pet 开了新会话但主页看不到").
//
// The fix has TWO parts, both asserted here:
// 1. id semantics: session.create returns `session_id` (runtime id for
//    prompt.submit) AND `stored_session_id` (durable id for openSession).
//    Passing the runtime id to openSession routes to a nonexistent session
//    → resume 404 → UI silently bounces back to the welcome draft.
// 2. ordering: the DB row only appears when the first prompt.submit
//    persists it, so openSession must fire AFTER submit.
//
// User-approved behavior (Zeyu, 2026-08-16): pet chat on a welcome draft
// SHOULD take over that blank view — chat there, continue there. Without
// this, a minimized Hermes + pet chat would silently spawn invisible
// sessions forever.

const here = path.dirname(fileURLToPath(import.meta.url))
const pluginPath = path.resolve(here, '..', '..', 'desktop-plugins', 'shorekeeper-pet', 'plugin.js')
let source = fs.readFileSync(pluginPath, 'utf8')
source = source
  .replace("import { host } from '@hermes/plugin-sdk'", 'const { host } = globalThis.__shorekeeperGhostSdk')
  .replace('export default {', 'globalThis.__shorekeeperGhostPlugin = {')

const rpcCalls = []
const openSessionCalls = []
const replyPosts = []
const notifications = []
let eventListener = null
let cleanup = null

const createdRuntimeId = 'runtime-abc123'
const createdStoredId = '20260816_041500_ghost99'
const requestId = 'pet-request-ghost-1'

// Desktop on a fresh New Session draft: activeSessionId is null.
globalThis.__shorekeeperGhostSdk = {
  host: {
    state: {
      activeSessionId: {
        get() {
          return null
        },
      },
    },
    async request(method, params) {
      rpcCalls.push({ method, params })
      if (method === 'session.create') {
        return { result: { session_id: createdRuntimeId, stored_session_id: createdStoredId } }
      }
      return { ok: true }
    },
    async openSession(sessionId, options) {
      openSessionCalls.push({ sessionId, options: options ?? null })
    },
    notify(input) {
      notifications.push(input)
    },
    onEvent(type, fn) {
      assert.equal(type, '*')
      eventListener = fn
      return () => {
        eventListener = null
      }
    },
  },
}

let outboxServed = false
globalThis.fetch = async (url, options = {}) => {
  const method = options.method || 'GET'
  if (url.endsWith('/chat/outbox') && method === 'GET') {
    const requests = outboxServed ? [] : [{ text: '在吗', session_id: '', request_id: requestId }]
    outboxServed = true
    return {
      ok: true,
      status: 200,
      async json() {
        return { requests }
      },
    }
  }
  if (url.endsWith('/chat/reply') && method === 'POST') {
    replyPosts.push(JSON.parse(options.body))
    return { ok: true, status: 200 }
  }
  if (url.endsWith('/event') && method === 'POST') {
    return { ok: true, status: 200 }
  }
  throw new Error(`unexpected fetch: ${method} ${url}`)
}

vm.runInThisContext(source, { filename: pluginPath })
const plugin = globalThis.__shorekeeperGhostPlugin
plugin.register({
  onDispose(fn) {
    cleanup = fn
  },
})

// first poll runs at OUTBOX_POLL_MS (700ms)
await new Promise(resolve => setTimeout(resolve, 900))

assert.equal(typeof eventListener, 'function')
assert.equal(typeof cleanup, 'function')

// 1. session.create was used as the fallback
assert.equal(
  rpcCalls.some(row => row.method === 'session.create'),
  true,
  'bridge should fall back to session.create when no active session',
)

// 2. THE FIX: host.openSession got the STORED id (not the runtime id)
assert.equal(openSessionCalls.length, 1, `expected exactly one openSession call, got ${openSessionCalls.length}`)
assert.equal(
  openSessionCalls[0].sessionId,
  createdStoredId,
  `openSession must receive the stored_session_id, got ${openSessionCalls[0].sessionId}`,
)

// 3. id split: the prompt went to the RUNTIME id, and openSession fired
//    only AFTER the submit (row persistence ordering)
const submitIndex = rpcCalls.findIndex(row => row.method === 'prompt.submit')
assert.ok(submitIndex >= 0, 'prompt.submit should have been called')
assert.equal(rpcCalls[submitIndex].params.session_id, createdRuntimeId)
assert.equal(rpcCalls[submitIndex].params.text, '在吗')

// 4. replies still stream back to the pet for that session
eventListener({
  type: 'message.delta',
  session_id: createdRuntimeId,
  payload: { text: '在' },
})
eventListener({
  type: 'message.complete',
  session_id: createdRuntimeId,
  payload: { text: '在呢' },
})
await new Promise(resolve => setImmediate(resolve))
assert.deepEqual(replyPosts.map(row => ({ phase: row.phase, text: row.text })), [
  { phase: 'delta', text: '在' },
  { phase: 'complete', text: '在呢' },
])

// 5. no warnings fired on the happy path
assert.equal(
  notifications.some(n => n.kind === 'warning'),
  false,
  `unexpected warnings: ${JSON.stringify(notifications)}`,
)

cleanup()

console.log(JSON.stringify({
  ok: true,
  createdStoredId,
  openSessionCalls,
  promptSubmit: { session_id: rpcCalls[submitIndex].params.session_id, text: rpcCalls[submitIndex].params.text },
}, null, 2))
