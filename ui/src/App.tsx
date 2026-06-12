import { useEffect, useRef } from 'react'
import { useChat } from './useChat'
import { exportChat } from './exportChat'
import { ChatForm } from './components/ChatForm'
import { ExchangeView } from './components/ExchangeView'
import './App.css'

export default function App() {
  const { exchanges, send, pending } = useChat()
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    // optional call: jsdom doesn't implement scrollIntoView
    endRef.current?.scrollIntoView?.({ behavior: 'smooth' })
  }, [exchanges])

  return (
    <div className="app">
      <header>
        <h1>D&amp;D 5e Sage</h1>
        <p className="tagline">Grounded answers from the rulebooks — with citations.</p>
        <button
          type="button"
          className="export-btn"
          onClick={() => exportChat(exchanges)}
          disabled={exchanges.length === 0}
          title="Download conversation as JSON"
        >
          Export ↓
        </button>
      </header>

      <main className="exchanges">
        {exchanges.length === 0 && (
          <p className="empty-state">
            Ask about a spell, a monster, a rule — the Sage answers only from its
            sources, and tells you when it can't.
          </p>
        )}
        {exchanges.map((e) => (
          <ExchangeView key={e.id} exchange={e} onRetry={send} />
        ))}
        <div ref={endRef} />
      </main>

      <footer>
        <ChatForm onSend={send} disabled={pending} />
      </footer>
    </div>
  )
}
