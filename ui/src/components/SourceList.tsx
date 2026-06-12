import type { Source } from '../api'

/** Collapsible citations — collapsed by default with a count badge. */
export function SourceList({ sources }: { sources: Source[] }) {
  if (sources.length === 0) return null
  return (
    <details className="sources">
      <summary>
        {sources.length} source{sources.length === 1 ? '' : 's'}
      </summary>
      <ul>
        {sources.map((s, i) => (
          <li key={`${s.book}-${s.entity ?? s.section ?? i}`}>
            <span className="source-ref">
              [{i + 1}] <strong>{s.entity ?? s.section ?? s.chapter ?? s.book}</strong>
              {' — '}
              <span className="source-book">{s.book}</span>
              {s.page != null && <span className="source-page">, p.{s.page}</span>}
            </span>
            <blockquote className="source-snippet">{s.snippet}</blockquote>
          </li>
        ))}
      </ul>
    </details>
  )
}
