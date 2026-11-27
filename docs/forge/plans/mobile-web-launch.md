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

- Send the canonical browser URL and short joining instructions. The default
  path is sign in, open **Your seats**, and choose **Join table** after the GM
  confirms the seat. A table QR, if provided, uses the canonical
  `/table?campaign_id=<opaque-id>` address. Account and seat authorization still
  apply. Use a neutral message for players who need help finding their seat.
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

| Check | Pass evidence |
| --- | --- |
| Distribution | Fresh Safari on an actual iPhone and Chrome on an actual Android phone can open the same QR or URL, complete sign-in, join, and use the browser and optional installed modes. Verify on Wi-Fi and cellular. |
| Table privacy | Confirmed player sees their own slot and the table slot. Verify offered, waiting, signed-out, removed, and wrong-account states against the access contract. A shared view only receives the table slot. Reopening after sign-out or switching accounts starts clean. |
| Live behavior | Two players receive a reveal, a GM can Stop it, and End clears all slots. Verify device lock and resume, backgrounding, network loss, reload, and rotation. Confirm stale projections clear and a fresh authorized snapshot is required. |
| Accessibility | Check VoiceOver and TalkBack, keyboard focus, 44 px app targets, visible status, 200% text scaling, high zoom, portrait and landscape, both themes, and 320–390 px widths. Record practical results with the actual devices. |
| Operations | External DNS, TLS and readiness checks are green. Document thresholds for error rate, table join and reveal latency, capacity, and spending, with owners and an escalation path. Record a rollback drill and verify recovery. |

Publish the build, test evidence, pilot decision, and support notes together.
