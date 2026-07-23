import { describe, expect, it, vi } from 'vitest'
import {
  browserFamily,
  buildWebVitalPoint,
  transmitMetricPoints,
  type MetricPoint,
} from './metrics'

const point = (value: number): MetricPoint => ({
  name: 'ui.client.error_count',
  kind: 'numeric',
  unit: 'count',
  value,
  labels: { route_template: '/', browser_family: 'chromium' },
})

describe('browser metrics', () => {
  it('maps Web Vitals to the canonical catalog without page content', () => {
    expect(
      buildWebVitalPoint('LCP', 1840.5, {
        route_template: '/',
        browser_family: 'chromium',
      }),
    ).toEqual({
      name: 'ui.web_vital.lcp_ms',
      kind: 'numeric',
      unit: 'ms',
      value: 1840.5,
      labels: { route_template: '/', browser_family: 'chromium' },
    })
  })

  it('bounds browser family values', () => {
    expect(browserFamily('Mozilla/5.0 Firefox/128')).toBe('firefox')
    expect(browserFamily('Mozilla/5.0 Chrome/128 Safari/537.36')).toBe('chromium')
    expect(browserFamily('unrecognized')).toBe('other')
  })

  it('chunks batches at 50 and prefers sendBeacon', async () => {
    const sendBeacon = vi.fn<
      (url: string, data?: BodyInit | null) => boolean
    >(() => true)
    const fetchImpl = vi.fn<typeof fetch>()

    await transmitMetricPoints(Array.from({ length: 51 }, (_, index) => point(index)), {
      navigator: { sendBeacon },
      fetchImpl,
    })

    expect(sendBeacon).toHaveBeenCalledTimes(2)
    expect(fetchImpl).not.toHaveBeenCalled()
    const bodies = await Promise.all(
      sendBeacon.mock.calls.map(([, body]) => (body as Blob).text()),
    )
    expect(bodies.map((body) => JSON.parse(body).points.length)).toEqual([50, 1])
  })

  it('falls back to keepalive fetch and never leaks transport failures', async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockRejectedValue(new Error('offline'))

    await expect(
      transmitMetricPoints([point(1)], {
        navigator: { sendBeacon: () => false },
        fetchImpl,
      }),
    ).resolves.toBeUndefined()

    expect(fetchImpl).toHaveBeenCalledWith(
      '/metrics/ui',
      expect.objectContaining({ method: 'POST', keepalive: true }),
    )
  })
})
