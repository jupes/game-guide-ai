export function isJsonResponse(response: Response): boolean {
  return response.headers.get('Content-Type')?.split(';')[0].trim().toLowerCase() === 'application/json'
}

export async function isSignedOutResponse(response: Response): Promise<boolean> {
  if (response.status !== 401 || !isJsonResponse(response)) return false
  try {
    const body = await response.json()
    return body?.detail === 'not signed in' && Object.keys(body).length === 1
  } catch {
    return false
  }
}
