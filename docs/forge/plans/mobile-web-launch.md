# Aetheril domain and mobile web launch

## Release path

Launch one HTTPS web app for GM Workbench and the player table. Players can open it
from a shared link on iPhone and Android. Home-screen installation is optional.
Start with an invitation-only pilot. Expand access only after the pilot passes
and the owner approves the audience, support coverage, and spending limits.
Audio capture, paid signup, and an app-store release each have their own gates.

| Milestone | Evidence required | Bead |
| --- | --- | --- |
| Register the domain | Owner account has DNS control, renewal protection, and independent recovery. The canonical hostname, pilot audience, and monthly budget are recorded. | `f6fv` |
| Prepare the HTTPS front door | End-to-end readiness, app authentication, TLS, direct-origin restrictions, internal callers, and rollback are proven. | `na7`, `jimb` |
| Prepare optional installation | Public icon and manifest work in browser and standalone modes. Install assets carry no account or campaign data. | `icqs` |
| Run the mobile pilot | An actual iPhone and Android phone complete the table acceptance checks over Wi-Fi and cellular. | `dleq` |
| Expand the audience | Owner reviews pilot defects, costs, capacity, privacy, support, and commercial scope. Run and record the rollback drill first. | `1kg.9.5`, `1kg.9.6`, `yje.6.6` |

## Register and prepare the domain

1. Shortlist names and confirm ordinary renewal and transfer pricing, DNS hosting
   options, and any naming rights before purchase. Register under the owner or
   company account. Enable multifactor authentication, registrar lock and
   auto-renew. Record access recovery contacts, renewal date, DNS operator,
   and ownership in the team's credential manager. Apply the registrar's
   DNSSEC and transfer procedures to the chosen arrangement. [ICANN registrant
   resources](https://www.icann.org/registrants)
2. Use `app.<owned-domain>` as the default candidate for the one canonical
   application origin. Reserve the apex for the landing page or a redirect.
   Use a separate test hostname and identity configuration for staging. Publish
   public URLs only after DNS, certificates, and authentication are ready.
3. Keep the UI, auth routes, table streams, and API on that same origin. For a
   durable Cloud Run deployment, price the global external Application Load
   Balancer with a regional serverless NEG and managed TLS. Direct Cloud Run
   domain mapping is Preview and has limitations. Google lists $0.025 per hour
   for the first five applicable forwarding rules, about $18.25 for a 730-hour
   month at that rate. Allow for additional routing charges, traffic, billing
   controls, and backend capacity. The pilot runbook records a $10 budget with
   an automatic billing detach. Verify its effective configuration and obtain
   budget approval before provisioning. A DNS record alone will leave the
   current Cloud Run IAM access requirements in place. [Cloud Run domain
   options](https://docs.cloud.google.com/run/docs/mapping-custom-domains) ·
   [Google Cloud pricing](https://cloud.google.com/vpc/network-pricing#lb) ·
   [Pilot infrastructure runbook](../../deploy-gcp.md)

## Rehearse cutover and rollback

1. Recheck serving revision, Cloud Run IAM, DNS ownership, quota, and database
   headroom. Preserve the known-good deployment, ingress, IAM, TLS, DNS, and
   OAuth settings. Inventory schedulers, monitoring, and internal callers
   before restricting the default service URL. Test a database restore and use
   migrations compatible with the preceding application revision during rollout.
2. Fit the front door before distributing the domain. Confirm valid TLS and
   hostname redirects, preserve same-origin checks and host-only Secure
   cookies, and verify the trusted proxy configuration against the deployed
   topology. Verify the old direct public entry can no longer serve the
   application. Verify approved health and internal callers still work.
3. Reissue invite and reset links on the canonical origin. Test fresh sign-in,
   expired or invalid links, logout, and login return from a table URL. Expect
   existing users to sign in again when changing hosts. If Google sign-in is
   enabled, register the exact HTTPS `/auth/google/callback` URL, verify the
   owned domain and public policy links, then test both browser and standalone
   sign-in. Check any enabled billing callbacks separately. [Google OAuth web
   server guide](https://developers.google.com/identity/protocols/oauth2/web-server) ·
   [Google OAuth policies](https://developers.google.com/identity/protocols/oauth2/policies)
4. From outside the hosting network, verify certificate and redirect behavior
   and require `/healthz` to report ready with current migrations. Confirm a
   normal authenticated request and the player join journey. Check that direct
   origin traffic cannot bypass the selected front door. Enable table SSE only
   after verifying the database budget covers the listener and async pool for
   each instance during revision overlap. Exercise degraded snapshot polling
   in the pilot as well as realtime if enabled.
5. Define rollback triggers before the event. Stop distribution if sign-in
   breaks, the app fails readiness, private content crosses account boundaries,
   or Stop and End fail to clear the table. First close affected access or
   disable the affected flag. Move traffic to the last known-good revision
   when needed. Restore the compatible OAuth, DNS, ingress, and IAM settings
   in safe order, restoring private IAM before reopening direct ingress. Set a
   DNS rollback expectation that accounts for cache lifetimes. Verify the
   restored route from a fresh device before sending a new link.

The existing provider-specific cutover draft and verification contract are tracked
on `na7`. That bead owns execution of the DNS, TLS, and infrastructure changes.

## Distribute the mobile web app

- Once the GM confirms a player's seat, the player can sign in, open **Your
  seats**, and choose **Join table**. At a live table, the GM can also open
  **Share table** and choose **Show QR code** so players can scan it, or
  **Copy link** to send the address. If copying is unavailable, select and
  copy the displayed address. The QR is drawn on the GM's device and contains
  the current origin and `/table?campaign_id=<opaque-id>`. For the external
  pilot, open the GM workspace on the approved canonical hostname before
  sharing. A player who scans while signed out can use **Sign in**, then
  **Your seats** to join, or reopen the same address after signing in.
- Put install guidance in optional help. iPhone Safari offers **Add to Home
  Screen** and **Open as Web App** on supported versions. Android Chrome
  provides its browser-controlled installation or shortcut menu for eligible
  pages. Make the same table link usable in a regular tab from a message or QR
  scanner. For embedded browsers with sign-in problems, show how to open the
  address in Safari or Chrome. [Apple iPhone guide](https://support.apple.com/en-us/guide/iphone/iphea86e5236/ios) ·
  [Chrome on Android](https://support.google.com/chrome/answer/9658361?co=GENIE.Platform%3DAndroid&hl=en)
- Give the manifest a stable same-origin identifier, a neutral launch URL,
  home-screen icons, and standalone display. Keep installation metadata public
  and static. Use the existing online table experience with clear waiting,
  reconnecting, and unavailable states. Any later service worker requires a
  separate cache design and privacy test before handling authenticated pages.
- Give the GM a one-page handout with the approved URL, the steps to confirm a
  seat and join, device instructions, a reconnect fallback, and a support
  contact. Start with one or two consenting tables. Record the tested build,
  devices, outcomes, and incidents before increasing invites.

## Launch acceptance

For the shared-device sign-out check, use the same seated account in two
tabs. Open a sample conversation in one and a private table handout in
the other. Use **Sign out** from the account menu. Confirm the table tab
drops the handout and stops playback. Reopen the app
and check that the next account cannot see the previous conversation title
or profile alias. Repeat in the supported phone browsers. The server asks
the browser to clear its HTTP cache and origin storage. If the browser ignores
that request, the signing-out tab removes the app's shared local storage and
its own app entries in session storage. The app stores conversation titles
in local storage and keeps table handouts in memory. The open table tab
discards its handout when it receives the identity change. HTTP cache
eviction depends on the browser honoring the response header.

To finish sharing with the other people at the table, use **Stop** or **End**
and wait for confirmation before signing out of the GM account.

| Check | Pass evidence |
| --- | --- |
| Distribution | Fresh Safari on an actual iPhone and Chrome on an actual Android phone can open the same QR or URL, complete sign-in, join, and use the browser and optional installed modes. Verify on Wi-Fi and cellular. |
| Table privacy | Confirmed player sees their own slot and the table slot. Verify offered, waiting, signed-out, removed, and wrong-account states against the access contract. A shared view only receives the table slot. Reopening after sign-out or switching accounts starts clean. |
| Live behavior | Two players receive a reveal, a GM can Stop it, and End clears all slots. Verify device lock and resume, backgrounding, network loss, reload, and rotation. Confirm stale projections clear and a fresh authorized snapshot is required. |
| Table sound | On iPhone and Android, start an ambience before the player taps. Verify silence until the tap, audible playback afterward, local mute, and a fresh prompt after reload. Check that a background listener can still hear while the document is hidden. Compare the GM’s listener count with what each person hears after locking, resuming, and leaving. Switch table audio off mid-cue and confirm every device falls silent. Record the physical volume and silent-switch settings alongside the result. |
| Accessibility | Check VoiceOver and TalkBack, keyboard focus, 44 px app targets, visible status, 200% text scaling, high zoom, portrait and landscape, both themes, and 320–390 px widths. Record practical results with the actual devices. |
| Operations | External DNS, TLS and readiness checks are green. Document thresholds for error rate, table join and reveal latency, capacity, and spending, with owners and an escalation path. Record a rollback drill and verify recovery. |

Publish the build, test evidence, pilot decision, and support notes together.
