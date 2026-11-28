import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import { TableAddressShare } from './TableAddressShare'

const chunk = vi.hoisted(() => {
  let reject!: (reason: Error) => void
  const request = vi.fn(() => new Promise<never>((_, rejectImport) => { reject = rejectImport }))
  return { request, fail: () => reject(new Error('Local module unavailable')) }
})
vi.mock('qrcode.react', () => chunk.request())
afterEach(() => vi.restoreAllMocks())

it('requests the QR encoder only on demand and preserves sharing if the local chunk cannot load', async () => {
  vi.spyOn(console, 'error').mockImplementation(() => {}) // Expected React boundary recovery.
  const user = userEvent.setup()
  const copy = vi.spyOn(navigator.clipboard, 'writeText').mockResolvedValue()
  const announce = vi.fn()
  render(<TableAddressShare campaignId="cmp_A" announce={announce} />)
  await user.click(screen.getByRole('button', { name: 'Share table' }))
  await user.click(screen.getByRole('button', { name: 'Copy link' }))
  expect(chunk.request).not.toHaveBeenCalled()
  await user.click(screen.getByRole('button', { name: 'Show QR code' }))
  expect(await screen.findByText('Loading QR code…')).toBeVisible()
  expect(announce).toHaveBeenLastCalledWith('Loading QR code…')
  await vi.waitFor(() => expect(chunk.request).toHaveBeenCalledOnce())
  await act(async () => { chunk.fail() })
  const fallback = 'QR code unavailable. Copy the table address instead.'
  expect(await screen.findByText(fallback)).toBeVisible()
  expect(announce).toHaveBeenLastCalledWith(fallback)
  await user.click(screen.getByRole('button', { name: 'Copy link' }))
  expect(copy).toHaveBeenCalledTimes(2)
  expect(copy).toHaveBeenLastCalledWith(`${window.location.origin}/table?campaign_id=cmp_A`)
})
