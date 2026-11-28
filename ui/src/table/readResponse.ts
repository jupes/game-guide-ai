// Normalize the two legacy HTTP-date forms to IMF-fixdate (RFC 9110 §5.6.7).
// Comparing with toUTCString also excludes permissive Date.parse inputs such
// as "January 2099" and impossible calendar days.
function httpDate(value: string, now: number): number {
  let canonical = value
  const old = /^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday), (\d{2})-([A-Z][a-z]{2})-(\d{2}) (\d{2}:\d{2}:\d{2}) GMT$/.exec(value)
  if (old) {
    const [, weekday, day, month, shortYear, time] = old
    const latestYear = new Date(now).getUTCFullYear() + 50
    let year = 100 * Math.floor(latestYear / 100) + Number(shortYear)
    if (year > latestYear) year -= 100
    canonical = `${weekday.slice(0, 3)}, ${day} ${month} ${year} ${time} GMT`
  } else {
    const asctime = /^([A-Z][a-z]{2}) ([A-Z][a-z]{2}) ([ \d]\d) (\d{2}:\d{2}:\d{2}) (\d{4})$/.exec(value)
    if (asctime) {
      const [, weekday, month, day, time, year] = asctime
      canonical = `${weekday}, ${day.trim().padStart(2, '0')} ${month} ${year} ${time} GMT`
    }
  }
  const parsed = Date.parse(canonical)
  return Number.isFinite(parsed) && new Date(parsed).toUTCString() === canonical ? parsed : NaN
}

/** Return the earliest time the table may read again after a 429. */
export function tableReadRetryAt(response: Response): number {
  const now = Date.now()
  const header = response.headers.get('Retry-After')?.trim() ?? ''
  if (/^\d+$/.test(header)) {
    const delay = Math.max(1000, Number(header) * 1000)
    return Math.min(Number.MAX_SAFE_INTEGER, now + delay)
  }
  const date = httpDate(header, now)
  return Number.isFinite(date) ? Math.max(now + 1000, date) : now + 5000
}
