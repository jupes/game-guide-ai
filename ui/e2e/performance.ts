import { promises as fs } from 'node:fs'
import path from 'node:path'
import type { Page } from '@playwright/test'

export type PerformanceMetricName =
  | 'ui.web_vital.ttfb_ms'
  | 'ui.web_vital.fcp_ms'
  | 'ui.web_vital.lcp_ms'
  | 'ui.web_vital.cls'

export type PerformanceMetrics = Record<PerformanceMetricName, number>
export type PerformanceBudgets = Record<PerformanceMetricName, number>

export interface PerformanceReport {
  generated_at: string
  metrics: Record<
    PerformanceMetricName,
    {
      value: number
      budget: number
      unit: 'ms' | 'ratio'
      passed: boolean
    }
  >
}

export async function installPerformanceObservers(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const vitals = {
      fcp: undefined as number | undefined,
      lcp: undefined as number | undefined,
      cls: 0,
    }
    ;(
      window as typeof window & {
        __e2eVitals: typeof vitals
      }
    ).__e2eVitals = vitals

    const observe = (
      type: string,
      handle: (entry: PerformanceEntry) => void,
    ): void => {
      try {
        const observer = new PerformanceObserver((list) => {
          list.getEntries().forEach(handle)
        })
        observer.observe({ type, buffered: true })
      } catch {
        // A missing browser entry type is surfaced later as a non-finite metric.
      }
    }
    observe('paint', (entry) => {
      if (entry.name === 'first-contentful-paint') vitals.fcp = entry.startTime
    })
    observe('largest-contentful-paint', (entry) => {
      vitals.lcp = entry.startTime
    })
    observe('layout-shift', (entry) => {
      const shift = entry as PerformanceEntry & {
        value?: number
        hadRecentInput?: boolean
      }
      if (!shift.hadRecentInput) vitals.cls += shift.value ?? 0
    })
  })
}

export async function collectPerformanceMetrics(
  page: Page,
): Promise<PerformanceMetrics> {
  await page.waitForTimeout(100)
  return page.evaluate(() => {
    const navigation = performance.getEntriesByType(
      'navigation',
    )[0] as PerformanceNavigationTiming
    const vitals = (
      window as typeof window & {
        __e2eVitals?: {
          fcp?: number
          lcp?: number
          cls: number
        }
      }
    ).__e2eVitals
    return {
      'ui.web_vital.ttfb_ms':
        navigation?.responseStart - navigation?.startTime,
      'ui.web_vital.fcp_ms': vitals?.fcp ?? Number.NaN,
      'ui.web_vital.lcp_ms': vitals?.lcp ?? Number.NaN,
      'ui.web_vital.cls': vitals?.cls ?? Number.NaN,
    }
  })
}

export async function writePerformanceArtifacts(
  metrics: PerformanceMetrics,
  budgets: PerformanceBudgets,
  outputDirectory: string,
): Promise<PerformanceReport> {
  const report: PerformanceReport = {
    generated_at: new Date().toISOString(),
    metrics: {
      'ui.web_vital.ttfb_ms': reportMetric(
        metrics['ui.web_vital.ttfb_ms'],
        budgets['ui.web_vital.ttfb_ms'],
        'ms',
      ),
      'ui.web_vital.fcp_ms': reportMetric(
        metrics['ui.web_vital.fcp_ms'],
        budgets['ui.web_vital.fcp_ms'],
        'ms',
      ),
      'ui.web_vital.lcp_ms': reportMetric(
        metrics['ui.web_vital.lcp_ms'],
        budgets['ui.web_vital.lcp_ms'],
        'ms',
      ),
      'ui.web_vital.cls': reportMetric(
        metrics['ui.web_vital.cls'],
        budgets['ui.web_vital.cls'],
        'ratio',
      ),
    },
  }
  const markdown = [
    '## UI performance',
    '',
    '| Metric | Value | Budget | Result |',
    '| --- | ---: | ---: | --- |',
    ...Object.entries(report.metrics).map(
      ([name, metric]) =>
        `| \`${name}\` | ${metric.value.toFixed(metric.unit === 'ratio' ? 3 : 1)} ${metric.unit} | ${metric.budget} ${metric.unit} | ${metric.passed ? 'PASS' : 'FAIL'} |`,
    ),
    '',
  ].join('\n')

  await fs.mkdir(outputDirectory, { recursive: true })
  await Promise.all([
    fs.writeFile(
      path.join(outputDirectory, 'performance.json'),
      JSON.stringify(report, null, 2),
      'utf-8',
    ),
    fs.writeFile(
      path.join(outputDirectory, 'performance.md'),
      markdown,
      'utf-8',
    ),
  ])
  if (process.env.GITHUB_STEP_SUMMARY) {
    await fs.appendFile(process.env.GITHUB_STEP_SUMMARY, markdown, 'utf-8')
  }
  return report
}

function reportMetric(
  value: number,
  budget: number,
  unit: 'ms' | 'ratio',
): PerformanceReport['metrics'][PerformanceMetricName] {
  return {
    value,
    budget,
    unit,
    passed: Number.isFinite(value) && value >= 0 && value <= budget,
  }
}
