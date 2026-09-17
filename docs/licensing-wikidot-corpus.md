# Licensing — dnd5e.wikidot.com corpus

Mirrors the `x5bz.5` licensing-posture precedent (see [`invite-copy.md`](invite-copy.md)) for the
second, non-PDF ingestion source added by `dnd-corpus-wikidot-expansion`
(`agent-forge-harness-4x66`). Required by that feature's own acceptance criteria before any
scraped content ships to testers.

> **Review note (2026-09-16).** The conclusions below were written for the closed, invite-gated
> pilot. The "safe to re-serve with attribution" conclusion and the suggestion that wikidot
> content could feed a public SRD tier should not be relied on for any wider audience. The
> [plan review](forge/reports/additive-retrieval-and-billing-plan-review.md) found that 422 of
> the 849 wikidot chunks overlap PDF book text, that the site hosts full entries from non-SRD
> books, and that Wikidot's terms make contributors responsible for what they post, so the
> site-wide CC BY-SA 3.0 footer cannot be assumed to license publisher-owned text. Whether any
> wikidot content may reach a public or paid tier is decided by `agent-forge-harness-yje.1.1`,
> with counsel input.

## The source and its license

Content ingested from [dnd5e.wikidot.com](https://dnd5e.wikidot.com) is licensed
**Creative Commons Attribution-ShareAlike 3.0 (CC BY-SA 3.0)** — confirmed on every page's footer
("Unless otherwise stated, the content of this page is licensed under Creative Commons
Attribution-ShareAlike 3.0 License") and directly verified against live pages during research and
implementation, not assumed. This is a uniform, site-wide license — unlike the PDF corpus (verbatim
WotC book text, closed-group-only per `x5bz.5`) or Roll20's compendium (mixed licensing across SRD,
WotC-licensed book text, and third-party publishers — dropped from scope, see
`agent-forge-harness-h310`), wikidot content is safe to ingest and re-serve **with attribution**.

## What CC BY-SA 3.0 requires of this app

- **Attribution**: every answer grounded in wikidot-sourced content must be traceable back to the
  source. `service/generate.py`'s `build_sources()` labels every wikidot-origin citation
  `"D&D 5e Wiki — dnd5e.wikidot.com (CC BY-SA 3.0)"` instead of a raw book slug — the same citation
  mechanism the PDF corpus already uses, no new UI surface needed.
- **Share-alike**: derivative works must carry the same license. This app does not redistribute the
  wikidot corpus as a standalone dataset — it's retrieved and quoted inline in chat answers, the
  same usage pattern as the PDF corpus's book text, gated the same way (see below).

## Where this content lives

Ingested under `book_slug=wikidot-5e`, `source_type=wiki`, into the **same closed, invite-gated
corpus** as the 11 PDF books (`x5bz.2`) — not the SRD-only public tier (`x5bz.5`'s standing
requirement for anything wider than the closed circle, tracked separately under `x5bz.6`). This
feature only widens the existing closed-tester corpus; it does not change who can reach the app.
CC BY-SA 3.0's permissive terms would make wikidot content a good candidate source for that public
SRD tier later, but that's a separately-scoped initiative, not a consequence of this one.

## Scope of what was crawled

Five namespaces confirmed to exist on the live site: spells, races (the site calls these
`lineage:` internally), classes, feats, and equipment. No forums, talk/user pages, changelogs, or
homebrew/community-submission sections. `monster`, `condition`, and a general `rule` namespace are
not present on this site at all — not a licensing decision, a factual finding (no bestiary section
anywhere in the site's master index; direct guesses at condition pages all 404). See
`docs/forge/plans/dnd-corpus-wikidot-expansion.md` for the full trail.
