import type { Route } from '@playwright/test'

// A valid silent MPEG-2 Layer III frame contains 576 samples at 16 kHz.
const frame = Buffer.concat([Buffer.from([0xff, 0xf3, 0x18, 0xc4]), Buffer.alloc(32)])
const sound = Buffer.concat(Array.from({ length: 445 }, () => frame))

export function serveCueSound(route: Route) {
  const range = /^bytes=(\d+)-(\d*)$/.exec(route.request().headers().range ?? '')
  const headers = { 'Accept-Ranges': 'bytes' }
  if (!range) return route.fulfill({ headers, contentType: 'audio/mpeg', body: sound })
  const start = Number(range[1])
  const end = Math.min(range[2] ? Number(range[2]) : sound.length - 1, sound.length - 1)
  return route.fulfill({ status: 206, contentType: 'audio/mpeg', body: sound.subarray(start, end + 1),
    headers: { ...headers, 'Content-Range': `bytes ${start}-${end}/${sound.length}` } })
}
