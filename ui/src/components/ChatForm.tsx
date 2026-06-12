import { useState } from 'react'
import type { FormEvent, KeyboardEvent } from 'react'

/** Prompt input — Enter submits, Shift+Enter inserts a newline; disabled while
 * a request is in flight; empty input never submits. */
export function ChatForm({
  onSend,
  disabled,
}: {
  onSend: (prompt: string) => void
  disabled: boolean
}) {
  const [value, setValue] = useState('')

  const submit = () => {
    const trimmed = value.trim()
    if (!trimmed || disabled) return
    onSend(trimmed)
    setValue('')
  }

  const onSubmit = (e: FormEvent) => {
    e.preventDefault()
    submit()
  }

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      submit()
    }
  }

  return (
    <form className="chat-form" onSubmit={onSubmit}>
      <textarea
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={onKeyDown}
        placeholder="Ask the archives… e.g. What does a Mind Flayer do with its tentacles?"
        rows={2}
        disabled={disabled}
      />
      <button type="submit" disabled={disabled || value.trim() === ''}>
        Ask
      </button>
    </form>
  )
}
