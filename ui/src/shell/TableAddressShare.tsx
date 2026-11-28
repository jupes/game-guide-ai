import { Component, lazy, Suspense, useEffect, useId, useRef, useState } from 'react'
import { Button } from '../ds/Button'
import { tableAddress } from '../table/address'
import './TableAddressShare.css'

type ShareProps = { address: string; announce?: (message: string) => void }
const QRCodeSVG = lazy(() => import('qrcode.react').then(module => ({ default: module.QRCodeSVG })))

function QrNotice({ message, announce }: { message: string; announce?: ShareProps['announce'] }) {
  useEffect(() => { announce?.(message) }, [announce, message])
  return <p>{message}</p>
}

// An address the encoder cannot represent must still be available to copy.
class TableQr extends Component<ShareProps, { failed: boolean }> {
  state = { failed: false }

  static getDerivedStateFromError() { return { failed: true } }

  render() {
    const { address, announce } = this.props
    if (this.state.failed) return <QrNotice message="QR code unavailable. Copy the table address instead." announce={announce} />
    return <Suspense fallback={<QrNotice message="Loading QR code…" announce={announce} />}>
      <QRCodeSVG value={address} level="M" size={256} marginSize={4}
        bgColor="#fff" fgColor="#000" role="img" aria-label="QR code for the player table" />
    </Suspense>
  }
}

function AddressActions({ address, announce }: ShareProps) {
  const [showQr, setShowQr] = useState(false)
  const [feedback, setFeedback] = useState('')
  const field = useRef<HTMLTextAreaElement>(null)
  const attempt = useRef(0)
  const qrId = useId()

  async function copy(trigger: HTMLButtonElement): Promise<void> {
    const input = field.current
    if (!input) return
    const sequence = ++attempt.current
    const focusedAtPress = input.ownerDocument.activeElement
    let copied = false
    try {
      await navigator.clipboard.writeText(address)
      copied = true
    } catch { /* Clipboard access can be unavailable or denied in this browser. */ }
    // Ignore completions from an earlier press or from a disclosure that closed.
    if (field.current !== input || !input.isConnected || attempt.current !== sequence) return
    const focused = input.ownerDocument.activeElement
    // A Safari tap may leave focus on the previous control. An intervening
    // focus change still belongs to the user while clipboard permission settles.
    if (!copied && !input.ownerDocument.hidden && (focused === trigger || focused === focusedAtPress)) {
      input.focus()
      input.select()
    }
    const message = copied ? 'Table link copied.' : 'Select the table address and copy it.'
    setFeedback(message)
    announce?.(message)
  }

  return <div className="table-address-share__contents">
    <label className="table-address-share__field">Table address
      <textarea readOnly ref={field} value={address} rows={3} spellCheck={false} dir="ltr"
        onFocus={event => event.currentTarget.select()} />
    </label>
    <div className="table-address-share__actions">
      <Button variant="tonal" onClick={event => void copy(event.currentTarget)}>Copy link</Button>
      <Button variant="outlined" aria-expanded={showQr} aria-controls={qrId} onClick={() => setShowQr(!showQr)}>
        {showQr ? 'Hide QR code' : 'Show QR code'}
      </Button>
    </div>
    <div className="table-address-share__result" role={announce ? undefined : 'status'} aria-atomic="false">
      {feedback && <p>{feedback}</p>}
      <div id={qrId} className="table-address-share__qr" hidden={!showQr}>
        {showQr && <TableQr address={address} announce={announce} />}
      </div>
    </div>
  </div>
}

function AddressDisclosure({ address, announce }: ShareProps) {
  const [open, setOpen] = useState(false)
  const trigger = useRef<HTMLButtonElement>(null)
  const panelId = useId()
  return <div className="table-address-share" onKeyDown={event => {
    if (open && event.key === 'Escape') {
      event.stopPropagation()
      event.preventDefault()
      setOpen(false)
      trigger.current?.focus()
    }
  }}>
    <Button variant="outlined" icon="share" ref={trigger} aria-expanded={open} aria-controls={panelId} onClick={() => setOpen(!open)}>
      Share table
    </Button>
    <div id={panelId} hidden={!open}>{open && <AddressActions address={address} announce={announce} />}</div>
  </div>
}

/** The host supplies its confirmed live campaign and keys by live session. */
export function TableAddressShare({ campaignId, announce }: { campaignId: string; announce?: (message: string) => void }) {
  const address = window.location.origin + tableAddress(campaignId)
  return <AddressDisclosure key={campaignId} address={address} announce={announce} />
}
