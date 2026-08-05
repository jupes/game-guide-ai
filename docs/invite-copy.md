# Invite copy — closed pilot

Templates for the message you send with a one-time invite link, plus the rules
that copy has to follow. These exist because the pilot's **licensing posture**
(decision `x5bz.5`, 2026-07-13) puts real constraints on how the app is
described and who may reach it.

## Why the wording is constrained

The corpus is verbatim text from books the pilot group already owns, and the
rules/spell channels quote it by design. That is defensible for a **tiny,
private circle of book owners** and not defensible as anything that looks like
a public product. So the copy must not read like a launch, must not imply a
license or affiliation the project does not have, and must make the
"don't forward this" expectation explicit rather than assumed.

## Rules for any invite you send

**Must:**

- State plainly that it is a **private test**, not a public or finished product.
- Say the link is **single-use and personal to the recipient** — forwarding it
  doesn't work, and sharing it is asked against.
- Assume/expect the recipient **owns the books** the corpus is built from.
- Be clear it is **free**, with no payment or subscription implied.

**Must not:**

- Use **D&D / Dungeons & Dragons / Wizards of the Coast branding**, logos, or
  product names as the *identity* of the thing you're inviting them to.
- Imply any **official status, license, partnership, or endorsement**.
- Be posted **publicly** — no social posts, forums, Discord servers beyond a
  direct message, or anywhere indexable.

**Standing rule:** anything wider than this closed circle requires the
**SRD-only serving mode first** (SRD 5.1 ingested as its own `book_slug`, the
public corpus filtered to it, CC-BY-4.0 attribution shown). Until that exists,
the invite list *is* the access-control boundary.

## Template — direct message / email

> **Subject:** Trying out a rules-lookup tool I built — want in?
>
> Hi <name>,
>
> I've been building a personal rules-lookup assistant for our tabletop games —
> you ask a rules, spell, or monster question and it answers from the books,
> with a citation for where each answer came from.
>
> It's a **private test** with a handful of people, not a product, and it's
> free. I'm inviting people who already own the books it draws on.
>
> Here's your personal sign-up link — it works **once**, just for you, so
> please don't forward or post it:
>
> <INVITE_LINK>
>
> You'll pick your own password on that page. Tell me what's wrong with it —
> wrong answers, confusing wording, anything that annoys you — that feedback is
> the whole point.
>
> Thanks!

## Template — short version (chat)

> Building a private rules-lookup assistant for our games — answers questions
> from the books with citations. Small closed test, free, for people who own
> the books. Here's your one-time sign-up link (personal to you, please don't
> share it): <INVITE_LINK>

## Generating the link

```bash
# From the repo root, with the Cloud SQL proxy running (see deploy-gcp.md):
DATABASE_URL="$PROXY" python -m service.admin_invites create \
  --role player \
  --base-url https://<service-url>

# A DM (unlocks the GM channel):
DATABASE_URL="$PROXY" python -m service.admin_invites create --role dm --base-url https://<service-url>
```

The command prints a `.../#invite=<token>` link (the token rides in the URL **fragment**, which browsers never send to the server, so it stays out of Cloud Run request logs). Each token is **single-use**
and expires (default 14 days, `--ttl-days` to change). To see outstanding
invites or take one back:

```bash
DATABASE_URL="$PROXY" python -m service.admin_invites list
DATABASE_URL="$PROXY" python -m service.admin_invites revoke <token>
```

**The role is fixed by the invite** — a tester cannot promote themselves to DM
in the UI; the server enforces the GM channel from their session.

## If a link leaks

Revoking, rotating the session secret, draining in-flight requests and
deleting the account are operational steps with a load-bearing order — they
live with the rest of the runbook, in
[`deploy-gcp.md` §10](deploy-gcp.md#10-incident--a-leaked-invite-or-a-compromised-account).
