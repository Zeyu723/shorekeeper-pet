import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import vm from 'node:vm'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const pluginPath = path.resolve(here, '..', '..', 'desktop-plugins', 'shorekeeper-pet', 'plugin.js')
let source = fs.readFileSync(pluginPath, 'utf8')
source = source
  .replace("import { host } from '@hermes/plugin-sdk'", 'const { host } = globalThis.__shorekeeperChatSdk')
  .replace('export default {', 'globalThis.__shorekeeperChatPlugin = {')

const rpcCalls = []
const replyPosts = []
const animationPosts = []
const notifications = []
let eventListener = null
let cleanup = null
let outboxServed = false
let outboxDrainedOnce = false

const activeSessionId = 'active-session-42'
const requestId = 'pet-request-1'
globalThis.__shorekeeperChatSdk = {
  host: {
    state: {
      activeSessionId: {
        get() {
          return activeSessionId
        },
      },
    },
    async request(method, params) {
      rpcCalls.push({ method, params })
      return { ok: true }
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

globalThis.fetch = async (url, options = {}) => {
  const method = options.method || 'GET'
  if (url.endsWith('/chat/outbox') && method === 'GET') {
    const requests = outboxServed
      ? []
      : [
          { text: '注入当前会话', session_id: '', request_id: 'pet-request-1' },
          { text: '第二条立刻发出', session_id: '', request_id: 'pet-request-2' },
        ]
    if (requests.length === 0 || outboxServed) {
      outboxServed = true
    } else {
      outboxServed = false  // serve both on first poll, then nothing
      outboxDrainedOnce = true
    }
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
    animationPosts.push(JSON.parse(options.body))
    return { ok: true, status: 200 }
  }
  throw new Error(`unexpected fetch: ${method} ${url}`)
}

vm.runInThisContext(source, { filename: pluginPath })
const plugin = globalThis.__shorekeeperChatPlugin
plugin.register({
  onDispose(fn) {
    cleanup = fn
  },
})

await new Promise(resolve => setTimeout(resolve, 850))
assert.equal(typeof eventListener, 'function')
assert.equal(typeof cleanup, 'function')
assert.deepEqual(rpcCalls, [{
  method: 'prompt.submit',
  params: {
    session_id: activeSessionId,
    text: '注入当前会话',
  },
}, {
  method: 'prompt.submit',
  params: {
    session_id: activeSessionId,
    text: '第二条立刻发出',
  },
}])
assert.equal(rpcCalls.some(row => row.method === 'session.create'), false)

// Both submits were accepted immediately — replies stream back in order,
// each tagged with its own request_id.
eventListener({
  type: 'message.delta',
  session_id: activeSessionId,
  payload: { text: '你' },
})
eventListener({
  type: 'message.delta',
  session_id: activeSessionId,
  payload: { text: '好' },
})
eventListener({
  type: 'message.complete',
  session_id: activeSessionId,
  payload: { text: '你好' },
})
eventListener({
  type: 'message.delta',
  session_id: activeSessionId,
  payload: { text: '第' },
})
eventListener({
  type: 'message.complete',
  session_id: activeSessionId,
  payload: { text: '第二条完成' },
})
await new Promise(resolve => setImmediate(resolve))

assert.deepEqual(replyPosts.map(row => ({ phase: row.phase, text: row.text, request_id: row.request_id })), [
  { phase: 'delta', text: '你', request_id: 'pet-request-1' },
  { phase: 'delta', text: '你好', request_id: 'pet-request-1' },
  { phase: 'complete', text: '你好', request_id: 'pet-request-1' },
  { phase: 'delta', text: '第', request_id: 'pet-request-2' },
  { phase: 'complete', text: '第二条完成', request_id: 'pet-request-2' },
])
assert.equal(animationPosts.some(row => row.event === 'response.output_text.delta'), true)
assert.equal(animationPosts.some(row => row.event === 'response.completed'), true)
assert.equal(notifications.at(-1)?.kind, 'success')

cleanup()

console.log(JSON.stringify({
  ok: true,
  activeSessionId,
  promptSubmit: rpcCalls[0],
  sessionCreateCalled: false,
  replies: replyPosts.map(row => ({ phase: row.phase, text: row.text })),
}, null, 2))
