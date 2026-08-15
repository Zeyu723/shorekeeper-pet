import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import vm from 'node:vm'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const pluginPath = path.resolve(here, '..', '..', 'desktop-plugins', 'shorekeeper-pet', 'plugin.js')
let source = fs.readFileSync(pluginPath, 'utf8')
source = source
  .replace("import { host } from '@hermes/plugin-sdk'", 'const { host } = globalThis.__shorekeeperSdk')
  .replace('export default {', 'globalThis.__shorekeeperPlugin = {')

const requests = []
const notifications = []
let listener = null
let listenerDisposed = false
let cleanup = null

globalThis.__shorekeeperSdk = {
  host: {
    notify(input) {
      notifications.push(input)
    },
    onEvent(type, fn) {
      assert.equal(type, '*')
      listener = fn
      return () => {
        listenerDisposed = true
      }
    },
  },
}

globalThis.fetch = async (_url, options) => {
  requests.push(JSON.parse(options.body))
  return { ok: true, status: 200 }
}

vm.runInThisContext(source, { filename: pluginPath })
const plugin = globalThis.__shorekeeperPlugin
assert.equal(plugin.id, 'shorekeeper-pet')
assert.equal(plugin.defaultEnabled, true)

plugin.register({
  onDispose(fn) {
    cleanup = fn
  },
})

await new Promise(resolve => setImmediate(resolve))
assert.equal(typeof listener, 'function')
assert.equal(typeof cleanup, 'function')
assert.deepEqual(requests.map(row => row.event), ['gateway:startup'])

listener({ type: 'message.start', session_id: 's1', payload: {} })
listener({ type: 'message.delta', session_id: 's1', payload: { text: 'a' } })
listener({ type: 'message.delta', session_id: 's1', payload: { text: 'b' } })
listener({ type: 'tool.generating', session_id: 's1', payload: { name: 'terminal' } })
listener({ type: 'tool.start', session_id: 's1', payload: { name: 'terminal', tool_id: 't1' } })
listener({ type: 'tool.complete', session_id: 's1', payload: { name: 'terminal', tool_id: 't1' } })
listener({ type: 'tool.complete', session_id: 's1', payload: { name: 'terminal', tool_id: 't2', is_error: true } })
listener({ type: 'message.complete', session_id: 's1', payload: { text: 'done' } })
listener({ type: 'error', session_id: 's1', payload: { message: 'boom' } })
listener({ type: 'session.info', session_id: 's1', payload: {} })

await new Promise(resolve => setImmediate(resolve))
assert.deepEqual(requests.map(row => row.event), [
  'gateway:startup',
  'response.created',
  'response.output_text.delta',
  'agent:step',
  'tool.started',
  'tool.completed',
  'tool.failed',
  'response.completed',
  'response.failed',
])
assert.equal(requests.filter(row => row.originalType === 'message.delta').length, 1)
assert.equal(requests.find(row => row.event === 'tool.started').toolName, 'terminal')
assert.equal(requests.find(row => row.event === 'tool.started').toolId, 't1')
assert.equal(notifications.at(-1)?.kind, 'success')

cleanup()
assert.equal(listenerDisposed, true)

console.log(JSON.stringify({
  ok: true,
  plugin: plugin.id,
  defaultEnabled: plugin.defaultEnabled,
  forwardedEvents: requests.map(row => row.event),
  coalescedMessageDeltas: 1,
  disposerVerified: listenerDisposed,
}, null, 2))
