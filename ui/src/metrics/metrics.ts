export type BrowserFamily = 'chromium' | 'firefox' | 'webkit' | 'other'
export type RuntimeEnvironment = 'local' | 'test' | 'ci' | 'staging' | 'production'
export type MetricMode = 'sage' | 'spell' | 'rules' | 'gm'

export interface MetricLabels {
  environment?: RuntimeEnvironment
  release?: string
  mode?: MetricMode
  route_template?: '/' | '/chat' | '/metrics/ui'
  browser_family?: BrowserFamily
}

export type MetricPoint =
  | {
      name:
        | 'ui.web_vital.ttfb_ms'
        | 'ui.web_vital.fcp_ms'
        | 'ui.web_vital.lcp_ms'
        | 'ui.web_vital.cls'
        | 'ui.interaction.chat_round_trip_ms'
        | 'ui.client.error_count'
      kind: 'numeric'
      unit: 'ms' | 'ratio' | 'count'
      value: number
      labels: MetricLabels
    }
  | {
      name: 'ui.interaction.chat_outcome'
      kind: 'categorical'
      unit: 'category'
      value: 'success' | 'http_error' | 'network_error' | 'aborted'
      labels: MetricLabels
    }

export type WebVitalName = 'TTFB' | 'FCP' | 'LCP' | 'CLS'

const WEB_VITALS = {
  TTFB: { name: 'ui.web_vital.ttfb_ms', unit: 'ms' },
  FCP: { name: 'ui.web_vital.fcp_ms', unit: 'ms' },
  LCP: { name: 'ui.web_vital.lcp_ms', unit: 'ms' },
  CLS: { name: 'ui.web_vital.cls', unit: 'ratio' },
} as const

export function browserFamily(userAgent: string): BrowserFamily {
  if (/firefox|fxios/i.test(userAgent)) return 'firefox'
  if (/chrome|crios|chromium|edg/i.test(userAgent)) return 'chromium'
  if (/applewebkit|safari/i.test(userAgent)) return 'webkit'
  return 'other'
}

function finiteNonNegative(value: number): number {
  return Number.isFinite(value) ? Math.max(0, value) : 0
}

export function buildWebVitalPoint(
  vital: WebVitalName,
  value: number,
  labels: MetricLabels,
): MetricPoint {
  const definition = WEB_VITALS[vital]
  return {
    name: definition.name,
    kind: 'numeric',
    unit: definition.unit,
    value: finiteNonNegative(value),
    labels,
  }
}

interface BeaconNavigator {
  sendBeacon?: (url: string, data?: BodyInit | null) => boolean
}

interface TransportOptions {
  navigator?: BeaconNavigator
  fetchImpl?: typeof fetch
}

export async function transmitMetricPoints(
  points: MetricPoint[],
  options: TransportOptions = {},
): Promise<void> {
  const beaconNavigator =
    options.navigator ?? (typeof navigator === 'undefined' ? undefined : navigator)
  const fetchImpl =
    options.fetchImpl ?? (typeof fetch === 'undefined' ? undefined : fetch)

  for (let start = 0; start < points.length; start += 50) {
    const body = JSON.stringify({ points: points.slice(start, start + 50) })
    const blob = new Blob([body], { type: 'application/json' })
    let sent: boolean
    try {
      sent = beaconNavigator?.sendBeacon?.('/metrics/ui', blob) ?? false
    } catch {
      sent = false
    }
    if (sent || fetchImpl === undefined) continue
    try {
      await fetchImpl('/metrics/ui', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body,
        keepalive: true,
      })
    } catch {
      // Metrics are best-effort and must never affect product behavior.
    }
  }
}

function runtimeEnvironment(mode: string): RuntimeEnvironment {
  if (mode === 'test') return 'test'
  if (mode === 'production') return 'production'
  if (mode === 'staging') return 'staging'
  if (mode === 'ci') return 'ci'
  return 'local'
}

export function runtimeMetricLabels(mode?: MetricMode): MetricLabels {
  const release = import.meta.env.VITE_RELEASE
  return {
    environment: runtimeEnvironment(import.meta.env.MODE),
    ...(typeof release === 'string' && /^[\w.-]{1,64}$/.test(release)
      ? { release }
      : {}),
    ...(mode === undefined ? {} : { mode }),
    route_template: '/',
    browser_family: browserFamily(
      typeof navigator === 'undefined' ? '' : navigator.userAgent,
    ),
  }
}

let queue: MetricPoint[] = []
let flushTimer: ReturnType<typeof setTimeout> | undefined

function flushQueue(): void {
  if (flushTimer !== undefined) {
    clearTimeout(flushTimer)
    flushTimer = undefined
  }
  if (queue.length === 0) return
  const points = queue
  queue = []
  void transmitMetricPoints(points)
}

export function recordMetric(point: MetricPoint): void {
  queue.push(point)
  if (queue.length >= 50) {
    flushQueue()
    return
  }
  flushTimer ??= setTimeout(flushQueue, 5_000)
}

let started = false

export function startMetrics(): void {
  if (started || typeof window === 'undefined' || typeof performance === 'undefined') {
    return
  }
  started = true
  const labels = runtimeMetricLabels()
  const navigation = performance.getEntriesByType('navigation')[0] as
    | PerformanceNavigationTiming
    | undefined
  if (navigation !== undefined) {
    recordMetric(
      buildWebVitalPoint(
        'TTFB',
        navigation.responseStart - navigation.startTime,
        labels,
      ),
    )
  }

  let lcp: number | undefined
  let cls = 0
  const observe = (
    type: string,
    handle: (entry: PerformanceEntry) => void,
  ): void => {
    if (typeof PerformanceObserver === 'undefined') return
    try {
      const observer = new PerformanceObserver((list) => {
        list.getEntries().forEach(handle)
      })
      observer.observe({ type, buffered: true })
    } catch {
      // Unsupported entry types are expected on older browsers.
    }
  }

  observe('paint', (entry) => {
    if (entry.name === 'first-contentful-paint') {
      recordMetric(buildWebVitalPoint('FCP', entry.startTime, labels))
    }
  })
  observe('largest-contentful-paint', (entry) => {
    lcp = entry.startTime
  })
  observe('layout-shift', (entry) => {
    const shift = entry as PerformanceEntry & {
      value?: number
      hadRecentInput?: boolean
    }
    if (!shift.hadRecentInput) cls += shift.value ?? 0
  })

  let finalized = false
  const finalizeVitals = (): void => {
    if (!finalized) {
      finalized = true
      if (lcp !== undefined) recordMetric(buildWebVitalPoint('LCP', lcp, labels))
      recordMetric(buildWebVitalPoint('CLS', cls, labels))
    }
    flushQueue()
  }
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') finalizeVitals()
  })
  window.addEventListener('pagehide', finalizeVitals)
  window.addEventListener('error', () => {
    recordMetric({
      name: 'ui.client.error_count',
      kind: 'numeric',
      unit: 'count',
      value: 1,
      labels,
    })
  })
  window.addEventListener('unhandledrejection', () => {
    recordMetric({
      name: 'ui.client.error_count',
      kind: 'numeric',
      unit: 'count',
      value: 1,
      labels,
    })
  })
}
