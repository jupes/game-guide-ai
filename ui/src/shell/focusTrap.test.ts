/**
 * focusTrap (agent-forge-harness-0rn) — LAYOUT-10's Tab wrap, T-FT-1..4.
 *
 * jsdom does not move focus on a synthetic Tab, so these call `wrapTab` with
 * a spy event and read `document.activeElement`: the claim is exactly "moved
 * focus and prevented the default, or did neither".
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { wrapTab } from './focusTrap'

function tab(shiftKey = false, key = 'Tab') {
  return { key, shiftKey, preventDefault: vi.fn() }
}

let container: HTMLDivElement

beforeEach(() => {
  container = document.createElement('div')
  container.tabIndex = -1
  container.innerHTML = `
    <button type="button" id="first">First</button>
    <input id="middle" aria-label="Middle" />
    <a href="#x" id="link">Link</a>
    <select id="last" aria-label="Last"><option>a</option></select>
    <select id="disabled" aria-label="Disabled" disabled><option>b</option></select>
  `
  document.body.append(container)
})

afterEach(() => {
  container.remove()
})

const byId = (id: string): HTMLElement => {
  const el = document.getElementById(id)
  if (el === null) throw new Error(`no #${id}`)
  return el
}

describe('wrapTab', () => {
  it('T-FT-1: Tab on the last stop wraps to the first', () => {
    byId('last').focus()
    const event = tab()
    expect(wrapTab(event, container)).toBe(true)
    expect(event.preventDefault).toHaveBeenCalledTimes(1)
    expect(document.activeElement).toBe(byId('first'))
  })

  it('T-FT-2: Shift+Tab on the first stop, or on the container, wraps to the last', () => {
    byId('first').focus()
    const fromFirst = tab(true)
    expect(wrapTab(fromFirst, container)).toBe(true)
    expect(fromFirst.preventDefault).toHaveBeenCalledTimes(1)
    expect(document.activeElement).toBe(byId('last'))

    container.focus()
    const fromContainer = tab(true)
    expect(wrapTab(fromContainer, container)).toBe(true)
    expect(document.activeElement).toBe(byId('last'))
  })

  it('T-FT-3: skips a disabled control placed last; the wrap target is the last enabled one', () => {
    // #disabled is the document's last focusable-looking control; a wrap to it
    // would strand focus on nothing.
    byId('first').focus()
    wrapTab(tab(true), container)
    expect(document.activeElement).toBe(byId('last'))
    expect(document.activeElement).not.toBe(byId('disabled'))
  })

  it('T-FT-4: leaves other keys, middle stops and an empty container alone', () => {
    byId('last').focus()
    const escape = tab(false, 'Escape')
    expect(wrapTab(escape, container)).toBe(false)
    expect(escape.preventDefault).not.toHaveBeenCalled()
    expect(document.activeElement).toBe(byId('last'))

    byId('middle').focus()
    const forward = tab()
    expect(wrapTab(forward, container)).toBe(false)
    const backward = tab(true)
    expect(wrapTab(backward, container)).toBe(false)
    expect(forward.preventDefault).not.toHaveBeenCalled()
    expect(backward.preventDefault).not.toHaveBeenCalled()
    expect(document.activeElement).toBe(byId('middle'))

    const empty = document.createElement('div')
    document.body.append(empty)
    const onEmpty = tab()
    expect(wrapTab(onEmpty, empty)).toBe(false)
    expect(onEmpty.preventDefault).not.toHaveBeenCalled()
    empty.remove()
  })
})
