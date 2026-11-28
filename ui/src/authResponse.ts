export function isJsonResponse(response: Response): boolean {
  return response.headers.get('Content-Type')?.split(';')[0].trim().toLowerCase() === 'application/json'
}

const signedOutReasons = {
  table: ['not signed in'],
  account: ['authentication required', 'invalid or expired session', 'account no longer exists'],
}

export async function isSignedOutResponse(response: Response, kind: keyof typeof signedOutReasons = 'table'): Promise<boolean> {
  if (response.status !== 401 || !isJsonResponse(response)) return false
  try {
    const body = await response.json()
    return signedOutReasons[kind].includes(body?.detail) && Object.keys(body).length === 1
  } catch {
    return false
  }
}
