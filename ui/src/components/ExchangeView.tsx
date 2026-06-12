import type { Exchange } from '../useChat'
import { SourceList } from './SourceList'

/** One prompt/answer exchange: pending spinner, grounded answer + sources,
 * distinctly-styled refusal, or error with retry. */
export function ExchangeView({
  exchange,
  onRetry,
}: {
  exchange: Exchange
  onRetry: (prompt: string) => void
}) {
  return (
    <section className="exchange">
      <div className="prompt">{exchange.prompt}</div>

      {exchange.status === 'pending' && (
        <div className="answer pending" role="status">
          <span className="spinner" aria-hidden="true" />
          Consulting the tomes…
        </div>
      )}

      {exchange.status === 'done' && exchange.response && (
        <>
          <div
            className={`answer${exchange.response.answerable ? '' : ' refusal'}`}
          >
            {exchange.response.answer}
          </div>
          {exchange.response.answerable && (
            <SourceList sources={exchange.response.sources} />
          )}
        </>
      )}

      {exchange.status === 'error' && (
        <div className="answer error">
          {exchange.error}
          <button type="button" onClick={() => onRetry(exchange.prompt)}>
            Retry
          </button>
        </div>
      )}
    </section>
  )
}
