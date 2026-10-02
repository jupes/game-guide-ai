/**
 * workspaceFragment.test.ts -- the fragment grammar's reader and writer
 * (agent-forge-harness-1kg.2.5, PR-1a; brief T1-15, critic item 20).
 */

import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  fragmentWithKeys,
  readCampaignRestore,
  readWorkspaceKeys,
  replaceWorkspaceKeys,
} from './workspaceFragment'

afterEach(() => {
  vi.restoreAllMocks()
  window.history.replaceState(null, '', '/')
})

describe('readWorkspaceKeys', () => {
  it('reads both keys, with or without the leading #', () => {
    const expected = { campaignId: 'cmp_A', conversationId: 'cnv_1', documentId: null }
    expect(readWorkspaceKeys('#campaign=cmp_A&conversation=cnv_1')).toEqual(expected)
    expect(readWorkspaceKeys('conversation=cnv_1&x=1&campaign=cmp_A')).toEqual(expected)
  })

  it('reads the document key (1kg.6.3, CANVAS-30)', () => {
    expect(readWorkspaceKeys('#campaign=cmp_A&document=doc_1')).toEqual({
      campaignId: 'cmp_A', conversationId: null, documentId: 'doc_1',
    })
    expect(readWorkspaceKeys('#document=doc_1&conversation=cnv_1&campaign=cmp_A')).toEqual({
      campaignId: 'cmp_A', conversationId: 'cnv_1', documentId: 'doc_1',
    })
  })

  it('never reads a document without a well-formed campaign, nor a malformed or percent-encoded one', () => {
    expect(readWorkspaceKeys('#document=doc_1')).toEqual({ campaignId: null, conversationId: null, documentId: null })
    expect(readWorkspaceKeys('#campaign=bad!&document=doc_1').documentId).toBeNull()
    expect(readWorkspaceKeys('#campaign=cmp_A&document=doc%5F1').documentId).toBeNull()
    expect(readWorkspaceKeys('#campaign=cmp_A&document=').documentId).toBeNull()
    expect(readWorkspaceKeys('#campaign=cmp_A&document=bad!&document=doc_2').documentId).toBe('doc_2')
  })

  it('takes the first WELL-FORMED occurrence of each key', () => {
    expect(readWorkspaceKeys('#campaign=bad!&campaign=cmp_B&campaign=cmp_C')).toEqual({
      campaignId: 'cmp_B', conversationId: null, documentId: null,
    })
  })

  it.each([
    ['an empty value', '#campaign='],
    ['no value at all', '#campaign'],
    ['a percent-encoded value (values are never decoded)', '#campaign=cmp%5FA'],
    ['a value past 64 characters', `#campaign=${'a'.repeat(65)}`],
    ['a query string passed by mistake', '?campaign=cmp_A'],
    ['nothing', ''],
  ])('%s is absent', (_label, hash) => {
    expect(readWorkspaceKeys(hash)).toEqual({ campaignId: null, conversationId: null, documentId: null })
  })

  it('decodes a pair NAME as the reserved-key scrub does, but not its value', () => {
    expect(readWorkspaceKeys('#camp%61ign=cmp_A').campaignId).toBe('cmp_A')
  })

  it('never reads a conversation without a well-formed campaign', () => {
    expect(readWorkspaceKeys('#conversation=cnv_1')).toEqual({ campaignId: null, conversationId: null, documentId: null })
    expect(readWorkspaceKeys('#campaign=no%20pe&conversation=cnv_1').conversationId).toBeNull()
  })
})

describe('fragmentWithKeys', () => {
  const A = { campaignId: 'cmp_A', conversationId: null, documentId: null }
  const NONE = { campaignId: null, conversationId: null, documentId: null }

  it('keeps foreign pairs byte for byte and in order, then appends campaign and conversation', () => {
    expect(fragmentWithKeys('#a=1&other=doc_x&section&q=%20+', { ...A, conversationId: 'cnv_1' }))
      .toBe('a=1&other=doc_x&section&q=%20+&campaign=cmp_A&conversation=cnv_1')
  })

  it('appends campaign, then conversation, then document (1kg.6.3)', () => {
    expect(fragmentWithKeys('#a=1', { campaignId: 'cmp_A', conversationId: 'cnv_1', documentId: 'doc_1' }))
      .toBe('a=1&campaign=cmp_A&conversation=cnv_1&document=doc_1')
    expect(fragmentWithKeys('', { ...A, documentId: 'doc_1' })).toBe('campaign=cmp_A&document=doc_1')
  })

  it('replaces a document pair instead of duplicating it, malformed and encoded names included', () => {
    expect(fragmentWithKeys('#document=old&a=1&docum%65nt=x&document=bad!', { ...A, documentId: 'doc_1' }))
      .toBe('a=1&campaign=cmp_A&document=doc_1')
  })

  it('writes no document without a campaign, and none that is not opaque', () => {
    expect(fragmentWithKeys('#a=1', { ...NONE, documentId: 'doc_1' })).toBe('a=1')
    expect(fragmentWithKeys('', { ...A, documentId: 'doc 1' })).toBe('campaign=cmp_A')
    expect(fragmentWithKeys('', { ...A, documentId: 'Ondrey the Wise/../' })).toBe('campaign=cmp_A')
  })

  it('removes every own pair -- duplicates, malformed and encoded names -- before writing its own', () => {
    expect(fragmentWithKeys('#campaign=old&a=1&conversation=bad!&camp%61ign=x&campaign=', A))
      .toBe('a=1&campaign=cmp_A')
  })

  it('never writes invite or token, carried over or not (critic item 20)', () => {
    expect(fragmentWithKeys('#invite=x&campaign=cmp_A', A)).toBe('campaign=cmp_A')
    expect(fragmentWithKeys('#tok%65n=T&a=1', NONE)).toBe('a=1')
  })

  it('writes no pair for a malformed id, and no conversation without a campaign', () => {
    expect(fragmentWithKeys('#a=1', { ...NONE, campaignId: 'no/pe', conversationId: 'cnv_1' })).toBe('a=1')
    expect(fragmentWithKeys('#a=1', { ...NONE, conversationId: 'cnv_1' })).toBe('a=1')
    expect(fragmentWithKeys('', { ...A, conversationId: 'cnv 1' })).toBe('campaign=cmp_A')
  })

  it('strips the own keys when the state has none', () => {
    expect(fragmentWithKeys('#campaign=cmp_A&conversation=cnv_1&document=doc_1', NONE)).toBe('')
  })
})

describe('replaceWorkspaceKeys', () => {
  it('replaces the entry, keeping the path, the query and history.state', () => {
    window.history.replaceState({ keep: 'me' }, '', '/workspace?x=1#a=1&other=doc_x')
    const lengthBefore = window.history.length
    const push = vi.spyOn(window.history, 'pushState')

    expect(replaceWorkspaceKeys({ campaignId: 'cmp_A', conversationId: 'cnv_1', documentId: 'doc_1' })).toBe(true)

    expect(window.location.pathname).toBe('/workspace')
    expect(window.location.search).toBe('?x=1')
    expect(window.location.hash).toBe('#a=1&other=doc_x&campaign=cmp_A&conversation=cnv_1&document=doc_1')
    expect(window.history.state).toEqual({ keep: 'me' })
    expect(window.history.length).toBe(lengthBefore)
    expect(push).not.toHaveBeenCalled()
  })

  it('writes nothing when the fragment would not change', () => {
    window.history.replaceState(null, '', '/workspace#a=1&campaign=cmp_A')
    const replace = vi.spyOn(window.history, 'replaceState')

    expect(replaceWorkspaceKeys({ campaignId: 'cmp_A', conversationId: null, documentId: null })).toBe(false)
    expect(replace).not.toHaveBeenCalled()
    // Positive control: the same spy does see a write that changes something.
    expect(replaceWorkspaceKeys({ campaignId: 'cmp_B', conversationId: null, documentId: null })).toBe(true)
    expect(replace).toHaveBeenCalledTimes(1)
  })

  it('drops the # entirely once nothing is left', () => {
    window.history.replaceState(null, '', '/workspace#campaign=cmp_A')
    replaceWorkspaceKeys({ campaignId: null, conversationId: null, documentId: null })
    expect(window.location.href.endsWith('/workspace')).toBe(true)
  })
})

describe('readCampaignRestore', () => {
  it('restores only the workspace path with a well-formed campaign', () => {
    expect(readCampaignRestore('/workspace', '#campaign=cmp_A&conversation=cnv_1'))
      .toEqual({ campaignId: 'cmp_A', conversationId: 'cnv_1', documentId: null })
    expect(readCampaignRestore('/workspace', '#campaign=cmp_A'))
      .toEqual({ campaignId: 'cmp_A', conversationId: null, documentId: null })
  })

  it('carries the document key (1kg.6.3)', () => {
    expect(readCampaignRestore('/workspace', '#campaign=cmp_A&document=doc_1'))
      .toEqual({ campaignId: 'cmp_A', conversationId: null, documentId: 'doc_1' })
    expect(readCampaignRestore('/workspace', '#document=doc_1')).toBeNull()
  })

  it.each([
    ['/workspace', ''],
    ['/workspace', '#conversation=cnv_1'],
    ['/workspace', '#campaign=b@d'],
    ['/', '#campaign=cmp_A'],
    ['/profile', '#campaign=cmp_A'],
    ['/workspace/', '#campaign=cmp_A'],
  ])('%s%s restores nothing', (pathname, hash) => {
    expect(readCampaignRestore(pathname, hash)).toBeNull()
  })
})
