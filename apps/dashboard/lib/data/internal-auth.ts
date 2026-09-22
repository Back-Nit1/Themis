/**
 * The safe internal boundary in front of the one dashboard mutation: recording an investigator's
 * review decision (approve/reject/request more evidence). The repository has no user
 * authentication system (see packages/contracts README "Review decision workflow"), so there is
 * no real "who is this investigator" check available to build here -- a bearer token the browser
 * would send back is not a boundary, it is a secret exposed in every page visitor's network tab.
 *
 * Two things stand in for it, matching prompt.md's explicit instruction to keep mutation disabled
 * and report the blocker rather than exposing an unauthenticated public endpoint:
 *  - The mutation is a React Server Action (app/review/actions.ts), never a plain `app/api/**`
 *    route. Server Actions run only on the server and Next.js rejects a request whose Origin
 *    header does not match the deployment, so this is not casually callable the way an open POST
 *    endpoint is (see Next.js docs: Server Actions are protected from cross-origin invocation).
 *  - reviewDecisionsEnabled() below is a server-only, off-by-default operator switch. Unset (the
 *    default), decisions stay disabled and the UI reports why, exactly as
 *    apps/dashboard/docs/workstreams/frontend.md already documents: "Review actions are
 *    disabled/read-only in every mode (no safe decision API exists)".
 */
export const REVIEW_DECISIONS_ENABLED_ENV = 'THEMIS_REVIEW_DECISIONS_ENABLED';

export function reviewDecisionsEnabled(env: Record<string, string | undefined> = process.env): boolean {
  return env[REVIEW_DECISIONS_ENABLED_ENV]?.trim().toLowerCase() === 'true';
}

export const REVIEW_DECISIONS_DISABLED_MESSAGE =
  `Review decisions are disabled: ${REVIEW_DECISIONS_ENABLED_ENV} is not set to "true" on the server. `
  + 'This is a deliberate blocker (no adequate authentication boundary exists yet for an open mutation), not a bug.';
