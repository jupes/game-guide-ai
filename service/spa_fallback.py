"""
Client-router server fallback: an ALLOWLIST, not a catch-all
(agent-forge-harness-y40).

Three client-side screens -- `/`, `/workspace`, `/profile` -- need to resolve
on a COLD load in the built SPA, not only in dev (where Vite's own dev-server
fallback already serves `index.html` for anything unmatched). In production
this app mounts the built `ui/dist` at "/" with `StaticFiles(html=True)`, and
a `StaticFiles` mount only answers a path that is a real file on disk --
`/profile` is not one, so it 404s today. `docs/adr/client-routing.md` has the
fact table this fixes and the two behaviour changes it causes.

The fallback is deliberately an ALLOWLIST rather than a catch-all
`/{path:path}` route: threat-model SEC-3 requires that Workbench routes not
enumerate, and a catch-all would swallow a mistyped API path into `text/html`
where a caller expected JSON (and would shadow `/assets/...`, MS-7).
`CLIENT_ROUTES` is the ONLY set of paths this answers for; everything else
keeps answering exactly what a service-only image answers today -- a JSON 404
produced by FastAPI's own default handler. This module registers no handler;
`service/app.py` installs `service.workbench_api`'s two application-wide ones
(`install_workbench`), and for anything that is not a Workbench route they
delegate to that same default, so the answer is unchanged.

`install_spa` must be the LAST thing `service/app.py` calls: the `StaticFiles`
mount it registers is a `Mount("/")`, which matches every path, so anything
registered after it becomes unreachable
(`service/tests/test_spa_fallback.py::test_spa_fallback_is_registered_after_every_api_route`
pins the order with an AST check over `service/app.py`).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

#: The three client screens. `ui/src/shell/routes.ts` is the client half of
#: this table; `tests/test_spa_routes_parity.py` fails if the two drift.
#: Exact-match only -- no parameters, no wildcards, no trailing slash.
CLIENT_ROUTES: tuple[str, ...] = ("/", "/workspace", "/profile")

#: Name prefix for each allowlist route `install_spa` registers. Exported so
#: a test can filter these (and the mount below) out of the live route table
#: deterministically -- never by `isinstance` alone, since a normal API route
#: and one of these look identical to it.
SPA_ROUTE_PREFIX = "spa:"

#: Name of the `StaticFiles` mount that serves everything else under `dist`
#: (chiefly `/assets/...`, the built bundle).
SPA_MOUNT_NAME = "ui"


def install_spa(app: FastAPI, dist: Path) -> None:
    """Serve the built SPA from `dist`, if it exists; otherwise do nothing.

    Call this LAST -- after every API route is registered. When `dist` is a
    directory, registers, in this order:

    1. one route per `CLIENT_ROUTES` entry, answering GET and HEAD with
       `dist / "index.html"`. Not guarded by `require_session`: the
       signed-out visitor needs this shell to be able to reach Login at all.
    2. a `StaticFiles` mount at "/", so `/assets/...` keeps serving the
       built bundle.

    When `dist` is not a directory (a service-only image, or any checkout
    that has not run `cd ui && bun run build`), this registers nothing --
    today's behaviour for that shape of deployment is unchanged.
    """
    if not dist.is_dir():
        return

    index_html = dist / "index.html"

    async def _serve_index_html() -> FileResponse:
        return FileResponse(index_html)

    for path in CLIENT_ROUTES:
        app.add_api_route(
            path,
            _serve_index_html,
            methods=["GET", "HEAD"],
            include_in_schema=False,
            name=f"{SPA_ROUTE_PREFIX}{path}",
        )

    app.mount("/", StaticFiles(directory=dist, html=True), name=SPA_MOUNT_NAME)
