import { act, fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import { TableAddressShare } from './TableAddressShare'

afterEach(() => vi.restoreAllMocks())

it('copies only the same-origin public table URL, regardless of the GM page address', async () => {
  const previous = window.location.href
  window.history.replaceState(null, '', '/private/gm?session=private-session#secret')
  try {
    const user = userEvent.setup()
    const request = vi.spyOn(window, 'fetch').mockRejectedValue(new Error('Unexpected network call'))
    const copy = vi.spyOn(navigator.clipboard, 'writeText').mockResolvedValue()
    render(<TableAddressShare campaignId="cmp_A-B_123" />)
    const share = screen.getByRole('button', { name: 'Share table', expanded: false })
    await user.tab()
    expect(share).toHaveFocus()
    await user.keyboard('{Enter}')
    const field = screen.getByRole('textbox', { name: 'Table address' })
    const address = `${window.location.origin}/table?campaign_id=cmp_A-B_123`
    expect(field).toHaveValue(address)
    expect(field).toHaveAttribute('readonly')
    expect(screen.queryByRole('img')).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Copy link' }))
    expect(copy).toHaveBeenCalledWith(address)
    expect(screen.getByRole('status')).toHaveTextContent('Table link copied.')
    await user.click(screen.getByRole('button', { name: 'Show QR code', expanded: false }))
    expect((await screen.findByRole('img', { name: 'QR code for the player table' })).tagName.toLowerCase()).toBe('svg')
    await user.click(screen.getByRole('button', { name: 'Hide QR code', expanded: true }))
    expect(screen.queryByRole('img')).toBeNull()
    expect(request).not.toHaveBeenCalled()
  } finally {
    window.history.replaceState(null, '', previous)
  }
})

it('selects the address for manual copying and uses the host announcer when clipboard access is denied', async () => {
  const user = userEvent.setup()
  vi.spyOn(navigator.clipboard, 'writeText').mockRejectedValue(new DOMException('Denied', 'NotAllowedError'))
  const announce = vi.fn()
  render(<TableAddressShare campaignId="cmp_A" announce={announce} />)
  await user.click(screen.getByRole('button', { name: 'Share table' }))
  await user.click(screen.getByRole('button', { name: 'Copy link' }))
  const field = screen.getByRole<HTMLTextAreaElement>('textbox', { name: 'Table address' })
  expect(field).toHaveFocus()
  expect([field.selectionStart, field.selectionEnd]).toEqual([0, field.value.length])
  expect(announce).toHaveBeenCalledWith('Select the table address and copy it.')
  expect(screen.getByText('Select the table address and copy it.')).toBeVisible()
  expect(screen.queryByRole('status')).toBeNull()
})

it.each([false, true])('handles a pointer tap which does not focus Copy (focus moved while pending: %s)', async (moveFocus) => {
  const user = userEvent.setup()
  let deny!: (reason: Error) => void
  vi.spyOn(navigator.clipboard, 'writeText').mockReturnValue(new Promise<void>((_, reject) => { deny = reject }))
  render(<TableAddressShare campaignId="cmp_A" />)
  const share = screen.getByRole('button', { name: 'Share table' })
  await user.click(share)
  // A tap in Safari can activate the button without giving it keyboard focus.
  fireEvent.click(screen.getByRole('button', { name: 'Copy link' }))
  expect(share).toHaveFocus()
  const qr = screen.getByRole('button', { name: 'Show QR code' })
  if (moveFocus) qr.focus()
  await act(async () => { deny(new Error('Clipboard unavailable')) })
  const field = screen.getByRole<HTMLTextAreaElement>('textbox', { name: 'Table address' })
  expect(moveFocus ? qr : field).toHaveFocus()
  if (!moveFocus) expect([field.selectionStart, field.selectionEnd]).toEqual([0, field.value.length])
})

it('consumes Escape inside the open share panel and then returns Escape to the enclosing dialog', async () => {
  const user = userEvent.setup()
  const onEscape = vi.fn()
  render(<div onKeyDown={event => { if (event.key === 'Escape') onEscape() }}>
    <TableAddressShare campaignId="cmp_A" />
  </div>)
  const share = screen.getByRole('button', { name: 'Share table' })
  await user.click(share)
  await user.click(screen.getByRole('button', { name: 'Show QR code' }))
  await user.keyboard('{Escape}')
  expect(screen.queryByRole('textbox')).toBeNull()
  expect(share).toHaveFocus()
  expect(onEscape).not.toHaveBeenCalled()
  await user.keyboard('{Escape}')
  expect(onEscape).toHaveBeenCalledOnce()
})

it.each(['disclosure', 'campaign', 'session'])('discards an outstanding clipboard result after a %s change', async (change) => {
  const user = userEvent.setup()
  let finish!: () => void
  vi.spyOn(navigator.clipboard, 'writeText').mockReturnValue(new Promise<void>(resolve => { finish = resolve }))
  const announce = vi.fn()
  const view = render(<TableAddressShare key="tss_1" campaignId="cmp_A" announce={announce} />)
  await user.click(screen.getByRole('button', { name: 'Share table' }))
  await user.click(screen.getByRole('button', { name: 'Copy link' }))
  if (change === 'disclosure') await user.click(screen.getByRole('button', { name: 'Share table' }))
  else view.rerender(<TableAddressShare key={change === 'session' ? 'tss_2' : 'tss_1'}
    campaignId={change === 'campaign' ? 'cmp_B' : 'cmp_A'} announce={announce} />)
  await user.click(screen.getByRole('button', { name: 'Share table', expanded: false }))
  await act(async () => { finish() })
  expect(announce).not.toHaveBeenCalled()
  expect(screen.queryByText('Table link copied.')).toBeNull()
})

it('keeps the address and copy action when the encoder reaches its capacity', async () => {
  const user = userEvent.setup()
  const copy = vi.spyOn(navigator.clipboard, 'writeText').mockResolvedValue()
  vi.spyOn(console, 'error').mockImplementation(() => {}) // React reports the expected boundary recovery in development.
  render(<TableAddressShare campaignId={'x'.repeat(5000)} />)
  await user.click(screen.getByRole('button', { name: 'Share table' }))
  await user.click(screen.getByRole('button', { name: 'Show QR code' }))
  expect(await screen.findByText('QR code unavailable. Copy the table address instead.')).toBeVisible()
  const field = screen.getByRole<HTMLTextAreaElement>('textbox', { name: 'Table address' })
  await user.click(screen.getByRole('button', { name: 'Copy link' }))
  expect(copy).toHaveBeenCalledWith(field.value)
})
