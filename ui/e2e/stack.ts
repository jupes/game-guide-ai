/**
 * Where the suite expects the application to be, and who started it.
 *
 * One definition, read by playwright.config.ts (baseURL), by global
 * setup/teardown (whether to drive Compose), and by fixtures.ts (which origin
 * counts as "ours" for the no-remote-subresource guard). Three readers of the
 * same fact is exactly where three copies of the same `??` expression would
 * have gone, and the guard would have been the one to drift.
 */

/**
 * Set when the caller has already started a stack and only wants Playwright
 * pointed at it — global setup then leaves Compose alone, and teardown does not
 * tear down a stack it never brought up. CI sets nothing, so CI still builds
 * and runs the production Compose stack exactly as before. Pointing this at a
 * stack that is not actually up fails loudly on the first navigation rather
 * than quietly passing.
 */
export const EXTERNAL_STACK_URL: string | null = process.env.E2E_BASE_URL ?? null

/** The Compose stack's published address (docker-compose.e2e.yml, ui: 4173:80). */
const COMPOSE_STACK_URL = 'http://127.0.0.1:4173'

export const BASE_URL: string = EXTERNAL_STACK_URL ?? COMPOSE_STACK_URL

/** Origin of the application under test — every other origin is remote. */
export const APP_ORIGIN: string = new URL(BASE_URL).origin
