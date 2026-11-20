// A snapshot can carry two documents. Bound unfinished lines and frames
// separately from the server's queue so a malformed response can't grow memory.
const MAX_EVENT_CHARS = 4 * 1024 * 1024

export async function readEventStream(
  body: ReadableStream<Uint8Array>,
  signal: AbortSignal,
  onBytes: () => void,
  onEvent: (name: string, data: string) => boolean,
): Promise<void> {
  const reader = body.getReader()
  const decoder = new TextDecoder('utf-8', { fatal: true })
  const abort = () => { void reader.cancel().catch(() => {}) }
  signal.addEventListener('abort', abort, { once: true })
  let line = ''
  let event = ''
  let lines: string[] = []
  let eventSize = 0
  let skipLf = false

  function lineReceived(value: string): boolean {
    if (value === '') {
      const keepReading = lines.length === 0 || onEvent(event, lines.join('\n'))
      event = ''
      lines = []
      eventSize = 0
      return keepReading
    }
    const colon = value.indexOf(':')
    const key = colon === -1 ? value : value.slice(0, colon)
    let field = colon === -1 ? '' : value.slice(colon + 1)
    if (field.startsWith(' ')) field = field.slice(1)
    if (key === 'event') event = field
    if (key === 'data') {
      eventSize += field.length + 1
      if (eventSize > MAX_EVENT_CHARS) throw new Error('Event stream frame exceeds its size limit')
      lines.push(field)
    }
    return true
  }

  try {
    while (!signal.aborted) {
      const { done, value } = await reader.read()
      if (done || signal.aborted) return
      if (value.length > 0) onBytes()
      let chunk = decoder.decode(value, { stream: true })
      if (chunk === '') continue
      if (skipLf && chunk.startsWith('\n')) chunk = chunk.slice(1)
      skipLf = chunk.endsWith('\r')
      let start = 0
      for (const match of chunk.matchAll(/\r\n|\r|\n/g)) {
        line += chunk.slice(start, match.index)
        if (line.length > MAX_EVENT_CHARS) throw new Error('Event stream line exceeds its size limit')
        if (signal.aborted || !lineReceived(line)) return
        line = ''
        start = match.index + match[0].length
      }
      line += chunk.slice(start)
      if (line.length > MAX_EVENT_CHARS) throw new Error('Event stream line exceeds its size limit')
    }
  } finally {
    signal.removeEventListener('abort', abort)
    abort()
    reader.releaseLock()
  }
}
