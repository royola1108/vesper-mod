import type { EngineInterface, Register } from 'claude-code'

// vesper-mod: the real hook. It sends one sentence from the web into a
// Claude Code session, nothing else.
// The service drops `{msg_id, text}` into
// <state_dir>/mod-inbox/<session_id>/<sha256(msg_id)>.json; this module takes it within about a
// second, writes its `taking` marker, reads the file, and hands it to the session:
//   idle -> $.prompt.submit({ text, asUser: true }); busy -> $.session.append(the user's busy form)
// Under this plugin's name this module writes those and exactly one other line, the nudge
// (function `nudge` below), a fixed sentence your frontend must filter out itself.
// No status, hint or notice rows.
// The user's text is never written to disk by this module: an ack holds
// {msg_id, uuid, door} only, or {msg_id, refused, door} when Claude Code refused.
// If the module dies after taking and before any ack, the service ends that sentence
// `unknown`: it is never resent.
// The same 1 s poll also follows `$.session.id()` (after `/clear` the id changes and
// `session.start` does not fire), so a change moves the inbox folder polled and `.alive`
// to the new id at once.
// Optional preview socket: if the install copy's `config.json` carries a `preview_url`,
// this module also POSTs a 250 ms snapshot of the current turn to it: its text and
// thinking blocks (a thinking block carries kind: 'thinking'), the tool calls started,
// and the uuids of the rows the turn answers. Best-effort, silent on failure; nothing is queued.
// No `preview_url` means no preview traffic at all. The demo service has no receiver;
// the frame protocol a receiver must accept is in `docs/advanced.md` section 7.

type Door = 'prompt' | 'note'
type Config = { state_dir: string; source_sha: string; preview_url?: string }

// Preview snapshot state: the current turn's blocks, keyed by
// (step, attempt, chunk index) the way the turn.step stream numbers them, plus the tool calls
// started and the uuids of the rows the turn answers.
// `kind` only on a thinking block; a text block carries none.
// Over the cap (32768 chars) the turn's text preview is withdrawn until the next turn
// starts, instead of growing without bound: frames go on with empty blocks.
export type PreviewBlock = { step: number; attempt: number; index: number; kind?: 'thinking'; text: string }
export type PreviewTool = { step: number; attempt: number; index: number; id: string; name: string }
const MAX_ITEMS = 32 // `inputs` and `tools`: the oldest leave first
const keep = <T>(list: T[], item: T) => { list.push(item); if (list.length > MAX_ITEMS) list.shift() }
export class Preview {
  // One load of this module: a hot reload starts `seq` over, so a receiver compares `seq` only within a `load_id`.
  readonly loadId = crypto.randomUUID()
  loadedAt = 0
  private seq = 0
  private blocks = new Map<string, PreviewBlock>()
  private tools: PreviewTool[] = []
  private inputs: string[] = []
  private staged: string[] = [] // door `prompt` rows, which arrive ahead of the turn.start that takes them
  private attempt = 0
  private turn: string | null = null
  private suppressed = false
  startTurn(turn: string | null) {
    this.turn = turn
    this.clear()
    this.inputs = this.staged
    this.staged = []
  }
  endTurn() {
    this.turn = null
    this.clear()
  }
  rebind() {
    this.turn = null
    this.clear()
    this.staged = []
  }
  private clear() {
    this.blocks.clear()
    this.tools = []
    this.inputs = []
    this.suppressed = false
  }
  beginStep(): number {
    this.attempt += 1
    return this.attempt
  }
  feed(step: number, attempt: number, index: number, text: string, kind?: 'thinking') {
    if (this.suppressed || text === '') return
    const key = `${step}:${attempt}:${index}`
    const block = this.blocks.get(key) ?? (kind ? { step, attempt, index, kind, text: '' } : { step, attempt, index, text: '' })
    block.text += text
    this.blocks.set(key, block)
    let total = 0
    for (const b of this.blocks.values()) total += b.text.length
    if (total > 32768) {
      this.blocks.clear()
      this.suppressed = true
    }
  }
  // A tool call started (name only, no arguments). Kept whether or not the text was withdrawn.
  tool(step: number, attempt: number, index: number, id: string, name: string) {
    keep(this.tools, { step, attempt, index, id, name })
  }
  // The rows a turn answers, by door: `prompt` (typed, or submitted by this plugin) comes ahead of the turn it
  // opens; `delivery` and this plugin's own busy `note` land inside the running turn. Never `prompt.submit`'s
  // turnId, which only says which turn was running when the line was sent.
  input(door: string, uuid: string, own: boolean) {
    if (door === 'prompt') keep(this.staged, uuid)
    else if (this.turn !== null && (door === 'delivery' || (door === 'note' && own))) keep(this.inputs, uuid)
  }
  failStep(attempt: number) {
    for (const [key, block] of this.blocks) {
      if (block.attempt === attempt) this.blocks.delete(key)
    }
    this.tools = this.tools.filter(t => t.attempt !== attempt)
  }
  // Worth a tick: something to show, or a withdrawal the receiver must clear.
  get live() {
    return this.blocks.size > 0 || this.tools.length > 0 || this.suppressed
  }
  // `seq` is stamped here, when the frame is built to be sent, so it rises in send order.
  frame(sessionId: string, done: boolean, reason?: string) {
    return {
      session_id: sessionId,
      load_id: this.loadId,
      loaded_at_ms: this.loadedAt,
      seq: ++this.seq,
      turn_id: this.turn,
      blocks: [...this.blocks.values()],
      inputs: [...this.inputs],
      tools: [...this.tools],
      done,
      ...(done ? { reason } : {}),
    }
  }
}

// The nudge line is a fixed sentinel; your frontend must filter it out itself.
export const NUDGE_TEXT = 'The web message above arrived after your last reply finished.'

const POLL_MS = 1000
const INBOX_FILE = /^[0-9a-f]{64}\.json$/


// Module state is in-memory only: a hot reload drops the old module's
// timers, and the new module starts its own single poller.
let started = false
let isBusy = false
let isScanning = false
// Main-loop model requests counted so far, and the count right after the user's latest busy append
// resolved in this turn (undefined: none). Equal at turn end = nothing read the user's line.
let mainSteps = 0
let appendedAtStep: number | undefined
let isBeating = false
let acksDir = ''
// The session id the poll is bound to: re-read on every tick, never fixed at start.
let boundId = ''
// Sentences handed to $.prompt.submit whose row has not arrived yet. Memory only: the user's
// text is never written to disk here, and a reload drops the list (no ack, so the
// service ends that sentence `unknown`; the `.taking` marker still stops a retake).
// A refused submit (`drop` / rejection) leaves the list and writes a refused ack instead.
// `kind` is not used in v1: a file without it is the user's sentence, bound by text equality.
export type Pending = { stem: string; msgId: string; text: string }
let pending: Pending[] = []
// Preview socket state: created only when the config carries a preview_url.
let preview: Preview | undefined
let previewUrl = ''
let previewSending = false

async function begin($: EngineInterface) {
  if (started) return
  started = true
  try {
    const config = JSON.parse(await $.fs.read(`${$.plugin.root}/config.json`)) as Config
    if (typeof config.state_dir !== 'string' || typeof config.source_sha !== 'string') {
      throw new Error('config.json: state_dir and source_sha are required')
    }
    if (typeof config.preview_url === 'string' && config.preview_url !== '') {
      try {
        const url = new URL(config.preview_url)
        // Frames carry the whole turn's text: this machine only.
        if ((url.protocol === 'http:' || url.protocol === 'https:')
          && ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)) {
          previewUrl = config.preview_url
          preview = new Preview()
          preview.loadedAt = await $.clock.now()
          $.clock.every(250, () => {
            void postPreview($, false)
          })
        }
      } catch {
        // an unusable or non-local preview_url means no preview, never a failed start
      }
    }
    boundId = await $.session.id()
    const acks = `${config.state_dir}/mod-acks`
    acksDir = acks
    $.clock.every(POLL_MS, () => {
      void tick($, config, acks)
    })
  } catch {
    started = false // config or session id not readable yet: the next event tries again
  }
}

// After `/clear` the process goes on under a NEW session id and `session.start` does not
// fire, so the id is read on every tick. A change moves the inbox folder polled, `.alive`,
// to the new id at once. A read that fails keeps the id we have.
async function follow($: EngineInterface) {
  try {
    const id = await $.session.id()
    if (typeof id === 'string' && id !== '' && id !== boundId) {
      boundId = id
      preview?.rebind()
    }
  } catch {
    // keep the bound id
  }
}

async function tick($: EngineInterface, config: Config, acks: string) {
  await follow($)
  const bound = boundId
  const inbox = `${config.state_dir}/mod-inbox/${bound}`
  if (!isBeating) {
    isBeating = true
    try {
      const at = await $.clock.now()
      await $.fs.write(`${inbox}/.alive`, JSON.stringify({ at, source_sha: config.source_sha }))
    } catch {
      // no heartbeat this second: the service reads that as "not bound"
    } finally {
      isBeating = false
    }
  }
  if (isScanning) return // a take is still in flight; the heartbeat above keeps going
  isScanning = true
  try {
    const entries = await $.fs.list(inbox)
    for (const entry of entries) {
      if (entry.kind !== 'file') continue
      if (INBOX_FILE.test(entry.name)) {
        await take($, inbox, acks, entry.name)
      }
    }
  } catch {
    // folder not there yet, or a read failed: next tick
  } finally {
    isScanning = false
  }
}

// One preview POST: the current turn's full snapshot (the done frame carries the turn's last state and why it ended). A tick sends nothing while
// a frame is in flight or there is nothing to show. Best-effort and never queued:
// a failed frame is simply replaced by the next tick's snapshot. The done frame
// goes out once per turn whatever a tick is doing; its body is built before the
// first await, so the caller may clear the blocks at once.
async function postPreview($: EngineInterface, done: boolean, reason?: string): Promise<void> {
  if (!preview || previewUrl === '') return
  if (!done && (previewSending || !preview.live)) return
  if (!done) previewSending = true
  try {
    await $.http.fetch(previewUrl, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(preview.frame(boundId, done, reason)),
    })
  } catch {
    // next tick sends the latest full snapshot
  } finally {
    if (!done) previewSending = false
  }
}

// The three steps of the nudge rule, as functions the hooks call.

// The take calls it once the user's busy append resolved.
export function noteAppended() {
  appendedAtStep = mainSteps
}

// One model request is about to be sent. Only the main loop counts: a subagent's request does
// not carry the main conversation's rows.
export function stepSeen(agentId: string | undefined) {
  if (agentId === undefined) mainSteps += 1
}

// A turn ended. Main loop only. Nudge once if the user's busy append was followed by no main-loop
// request and the turn ended in an answer (after an interrupt the user stopped it on purpose; after
// an error or refusal we start nothing); whatever the reason, the record is spent.
export function completed($: EngineInterface, e: { reason: string; agentId?: string }) {
  if (e.agentId !== undefined) return
  const due = e.reason === 'answer' && appendedAtStep !== undefined && appendedAtStep === mainSteps
  appendedAtStep = undefined
  if (due) nudge($)
}

// The only other write of a row: one fixed line so a new turn starts and reads the user's line above.
// No `asUser` (the transcript's `origin` then differs from the user's idle line). Not awaited: submit
// resolves when its turn starts.
// Your frontend filters it by origin.kind plugin + no asUser + not isMeta (busy user lines are isMeta).
function nudge($: EngineInterface) {
  $.prompt.submit({ text: NUDGE_TEXT }).then(undefined, () => {})
}

// Inbox format: what one inbox file asks for. A file without `kind` is the user's
// sentence (the service writes none). Anything malformed, or a `kind` this module does not know,
// is nothing: the take wrote its marker and stops (the service ends it `unknown`, never guessed).
export type InboxEntry = { kind: 'user'; msgId: string; text: string }

export function parseInbox(body: unknown): InboxEntry | undefined {
  const b = body as { msg_id?: unknown; text?: unknown; kind?: unknown } | null
  if (!b || typeof b !== 'object' || typeof b.msg_id !== 'string' || typeof b.text !== 'string') return undefined
  if (b.kind !== undefined && b.kind !== 'user') return undefined
  return { kind: 'user', msgId: b.msg_id, text: b.text }
}

// The only place that writes the user's sentence under this plugin's name.
// The marker FIRST, THEN the read: the text handed on comes from a read made after the
// marker exists, so a file the service removed in between is never submitted.
async function take($: EngineInterface, inbox: string, acks: string, name: string) {
  const stem = name.slice(0, -'.json'.length)
  if ((await $.fs.exists(`${acks}/${stem}.taking`)) || (await $.fs.exists(`${acks}/${stem}.json`))) return
  await $.fs.write(`${acks}/${stem}.taking`, '1') // before anything else; never retaken
  const entry = parseInbox(JSON.parse(await $.fs.read(`${inbox}/${name}`)))
  if (entry === undefined) return
  const write = (path: string, body: string) => $.fs.write(path, body)

  const text = entry.text

  if (isBusy) {
    const result = await $.session.append({ message: { type: 'user', content: [{ type: 'text', text }] } })
    noteAppended() // after the append resolved: a step racing it counts as "before" (one extra nudge at worst)
    const door: Door = 'note'
    await $.fs.write(`${acks}/${stem}.json`, JSON.stringify({ msg_id: entry.msgId, uuid: result.uuid, door }))
  } else {
    // Not awaited: submit resolves when the turn starts, which can be long after a busy
    // moment; the row (and so the ack) is caught by the session.append hook below.
    const waiting: Pending = { stem, msgId: entry.msgId, text }
    pending.push(waiting)
    // A refusal (`result.drop`, or a rejected promise) is final: Claude Code 2.1.299 refuses a plugin's
    // submit after the session was stopped until a person sends a sentence. `refuseSubmit` says so in the ack
    // (`refused`, no uuid) so the service ends the sentence `refused` at once; it is never submitted again.
    $.prompt.submit({ text, asUser: true }).then(
      result => {
        if (result.drop !== undefined) void refuseSubmit(write, acks, waiting, refusalText(result.drop))
      },
      () => void refuseSubmit(write, acks, waiting, undefined),
    )
  }
}

// Refused ack: `{msg_id, refused, door: 'submit'}` (no uuid, and no user text).
export const SUBMIT_REJECTED = 'submit_rejected'
const REFUSED_MAX = 200

function refusalText(drop: unknown): string | undefined {
  return typeof drop === 'string' && drop !== '' ? drop : undefined
}

// ONLY while the entry is still pending: once its row landed, `ackRow` took the entry and wrote the
// consumed ack — the sentence WAS taken, and a late drop / rejection must not overwrite that ack.
// Never throws: a failed write leaves no ack, and the service ends that sentence `unknown` as before.
export async function refuseSubmit(
  write: (path: string, text: string) => Promise<void>,
  acks: string,
  entry: Pending,
  reason: string | undefined,
) {
  if (!pending.includes(entry)) return
  pending = pending.filter(p => p !== entry)
  try {
    const refused = (reason ?? SUBMIT_REJECTED).slice(0, REFUSED_MAX)
    await write(`${acks}/${entry.stem}.json`, JSON.stringify({ msg_id: entry.msgId, refused, door: 'submit' }))
  } catch {
    // no ack: the service ends this sentence `unknown`
  }
}

export async function ackRow(
  write: (path: string, text: string) => Promise<void>,
  e: { door: string; agentId?: string; uuid: string; origin: { kind: string; name?: string }; message: { content: unknown; isMeta?: unknown } },
  pluginName: string,
) {
  try {
    if (e.door !== 'prompt' || e.agentId !== undefined || e.origin.kind !== 'plugin' || e.origin.name !== pluginName) return
    const text = rowText(e.message.content)
    const entry = pending.find(p => p.text === text)
    if (entry === undefined) return
    pending = pending.filter(p => p !== entry)
    await write(`${acksDir}/${entry.stem}.json`, JSON.stringify({ msg_id: entry.msgId, uuid: e.uuid, door: 'prompt' }))
  } catch {
    // no ack: the service ends this sentence `unknown`
  }
}

function rowText(content: unknown): string {
  if (typeof content === 'string') return content
  if (!Array.isArray(content)) return ''
  return content
    .map(b => (b && typeof b === 'object' && (b as { type?: unknown }).type === 'text' ? String((b as { text?: unknown }).text ?? '') : ''))
    .join('')
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await begin($)
    return next(e)
  })

  on('turn.start', async ($, e, next) => {
    // Main loop only, as turn.complete: a subagent's turn would set it and never clear it.
    if (e.agentId === undefined) {
      isBusy = true
      const turnId = (e as { turnId?: unknown }).turnId
      preview?.startTurn(typeof turnId === 'string' ? turnId : null)
    }
    await begin($) // a hot load can miss session.start
    return next(e)
  })

  // Counted before `next`: the hook fires as the engine is about to send that request.
  // With preview on, the main loop's stream is also read for text and thinking chunks
  // (each extends its block) and tool chunks (name and id only); the 250 ms ticker posts the whole snapshot.
  // A thinking block is recorded with kind: 'thinking'; a text block carries no kind.
  // A failed step drops its blocks and tools.
  on('turn.step', async function* ($, e, next) {
    stepSeen(e.agentId)
    if (e.agentId !== undefined || !preview) return yield* next(e)
    const attempt = preview.beginStep()
    const stepIndex = e.index
    const stream = next(e)
    try {
      for await (const chunk of stream) {
        const c = chunk as { kind?: unknown; index?: unknown; text?: unknown; id?: unknown; name?: unknown } | null
        if (c && typeof c.index === 'number') {
          try {
            if ((c.kind === 'text' || c.kind === 'thinking') && typeof c.text === 'string') {
              preview.feed(stepIndex, attempt, c.index, c.text, c.kind === 'thinking' ? 'thinking' : undefined)
            } else if (c.kind === 'tool' && typeof c.id === 'string' && typeof c.name === 'string') {
              preview.tool(stepIndex, attempt, c.index, c.id, c.name)
            }
          } catch {
            // observation only: never disturb the stream
          }
        }
        yield chunk
      }
      return await stream.result
    } catch (error) {
      preview.failStep(attempt)
      throw error
    }
  })

  on('turn.complete', async ($, e, next) => {
    if (e.agentId === undefined) isBusy = false
    const result = await next(e)
    completed($, e)
    if (e.agentId === undefined) {
      void postPreview($, true, typeof e.reason === 'string' ? e.reason : undefined) // no network await in a hook
      preview?.endTurn()
    }
    return result
  })

  // The row our own submit caused arrives here (door `prompt`, origin this plugin, main loop).
  // The engine keeps the row FIRST (`next`); the ack is written after, so an ack never exists
  // for a row the engine did not keep, and a failed ack write can never make this gating hook
  // refuse the user's row (`ackRow` swallows it: the service then ends that sentence `unknown`).
  on('session.append', async ($, e, next) => {
    const kept = await next(e)
    if (preview && e.agentId === undefined && kept.deny === undefined) {
      try { preview.input(e.door, kept.uuid, e.origin?.kind === 'plugin' && e.origin.name === $.plugin.name) } catch { /* observation only */ }
    }
    await ackRow((path, text) => $.fs.write(path, text), e, $.plugin.name)
    return kept
  })
}
