const CAMPAIGN_ID = /^[A-Za-z0-9_-]{1,64}$/

export function tableAddress(campaignId: string): string {
  return `/table?campaign_id=${encodeURIComponent(campaignId)}`
}

/** The address carries one opaque campaign ID and grants no access. */
export function tableCampaign(location: Location, history: History): string | null {
  const ids = new URLSearchParams(location.search).getAll('campaign_id')
  const id = ids.length === 1 && CAMPAIGN_ID.test(ids[0]) ? ids[0] : null
  const address = id === null ? '/table' : tableAddress(id)
  if (location.pathname + location.search + location.hash !== address) history.replaceState(null, '', address)
  return id
}
