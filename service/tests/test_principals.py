"""The principal resolver's interface and constraints at the database boundary."""

from __future__ import annotations

import ast
import inspect
import re
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
from psycopg_pool import PoolClosed, PoolTimeout

from service import campaign_identity as ident
from service import policy, policy_facts, principals
from service.principals import PrincipalFactsStore, PrincipalResolver
from service.session import SessionData
from service.tests.test_policy import minting_violations
from service.tests.test_table_api import FORBIDDEN_IMPORTS, _imported_modules


def test_authentication_precedes_invalid_campaign_and_request_data_cannot_supply_identity() -> None:
    resolver = PrincipalResolver(None, None)
    account = SessionData(1, "dm")
    campaign = ident.new_id(ident.CAMPAIGN)
    assert resolver.table("invalid", account=None, screen=None) is policy.TableRefusal.UNAUTHENTICATED
    assert resolver.table("invalid", account=account, screen=None) is policy.TableRefusal.INACTIVE
    with pytest.raises(TypeError):
        resolver.table(campaign, account=SimpleNamespace(user_id=account.user_id), screen=None)
    with pytest.raises(TypeError):
        resolver.table(campaign, account=None, screen=SimpleNamespace(grant_id=ident.new_id(ident.TABLE_CREDENTIAL)))
    with pytest.raises(TypeError):
        resolver.gm(campaign, account=SimpleNamespace(user_id=account.user_id))


def test_resolver_signatures_accept_no_request_claims() -> None:
    calls = {
        PrincipalResolver.__init__: "(self, db: 'TransactionalDatabase', facts: 'PrincipalFactsStore', *, "
                                    "clock: 'Callable[[], datetime] | None' = None) -> 'None'",
        PrincipalResolver.table: "(self, campaign_id: 'str', *, account: 'SessionData | None', "
                                 "screen: 'LiveScreen | None', now: 'datetime | None' = None) -> 'TableOutcome'",
        PrincipalResolver.gm: "(self, campaign_id: 'str', *, account: 'SessionData') -> 'GmPrincipal'",
        PrincipalFactsStore.table_facts: "(self, unit: 'UnitOfWork', campaign_id: 'str', *, "
                                        "user_id: 'int | None', grant_id: 'str | None') -> 'TableFacts'",
        PrincipalFactsStore.gm_facts: "(self, unit: 'UnitOfWork', campaign_id: 'str', *, "
                                     "user_id: 'int') -> 'GmFacts | None'",
    }
    for method, signature in calls.items():
        assert str(inspect.signature(method)) == signature


@pytest.mark.parametrize("site", ["acquire", "statement", "rollback", "commit"])
@pytest.mark.parametrize("method", ["gm", "table"])
def test_operational_errors_from_the_whole_transaction_are_unavailable(site, method) -> None:
    error = {
        "acquire": PoolClosed,
        "statement": psycopg.errors.QueryCanceled,
        "rollback": psycopg.errors.AdminShutdown,
        "commit": psycopg.OperationalError,
    }[site]
    campaign = ident.new_id(ident.CAMPAIGN)
    account = SessionData(1, "dm")

    class Db:
        @contextmanager
        def transaction(self):
            if site == "acquire":
                raise error("private acquire details")
            try:
                yield None
            except psycopg.Error as exc:
                if site == "rollback":
                    raise error("private rollback details") from exc
                raise
            if site == "commit":
                raise error("private commit details")

    class Facts:
        def gm_facts(self, *_args, **_kwargs):
            if site in {"statement", "rollback"}:
                raise psycopg.errors.QueryCanceled("private statement details")
            return policy.GmFacts(account.user_id, False, 0, 0)

        def table_facts(self, *_args, **_kwargs):
            self.gm_facts()
            return policy.TableFacts(campaign, account.user_id, (), (), None, {}, {}, (0, 0))

    resolver = PrincipalResolver(Db(), Facts())
    with pytest.raises(policy_facts.PolicyUnavailable) as exc:
        if method == "table":
            resolver.table(campaign, account=account, screen=None)
        else:
            resolver.gm(campaign, account=account)
    assert exc.value.__context__ is None and exc.value.__cause__ is None
    assert str(exc.value) == "Campaign policy is temporarily unavailable"


def test_pool_timeout_and_programming_error_keep_their_distinct_contracts() -> None:
    class Db:
        @contextmanager
        def transaction(self):
            raise error
            yield

    account = SessionData(1, "dm")
    campaign = ident.new_id(ident.CAMPAIGN)
    resolver = PrincipalResolver(Db(), None)
    error = PoolTimeout("internal details")
    with pytest.raises(policy_facts.PolicyUnavailable):
        resolver.gm(campaign, account=account)
    error = psycopg.ProgrammingError("internal details")
    with pytest.raises(psycopg.ProgrammingError) as exc:
        resolver.gm(campaign, account=account)
    assert exc.value is error


@pytest.mark.parametrize(
    "source",
    [
        "from service.session import SessionData as S\nS(1, 'dm')",
        "from service import table_session_store as sessions\nsessions.LiveScreen(a, b, c, d)",
        "from service.table_api import Principal as P\nP(account=x)",
    ],
)
def test_existing_minting_scan_rejects_planted_identity_sources(source: str) -> None:
    assert minting_violations("service/untrusted_route.py", source)


def _read_only(sql: str) -> bool:
    return bool(re.match(r"^\s*(WITH|SELECT)\b", sql, re.I)) and not re.search(
        r"\b(INSERT|UPDATE|DELETE|FOR SHARE|FOR KEY SHARE|FOR NO KEY|set_config|pg_advisory\w*|pg_notify|nextval)\b",
        sql,
        re.I,
    )


def test_loaders_have_only_read_statements_and_no_gm_store_imports() -> None:
    assert not _read_only("SELECT id FROM campaign.documents FOR SHARE")
    for module, count in ((principals, 2), (policy_facts, 1)):
        source = Path(module.__file__).read_text(encoding="utf-8")
        sql = [
            n.value.value for n in ast.parse(source).body
            if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id.endswith("_SQL")
            and isinstance(n.value, ast.Constant)
        ]
        assert len(sql) == count and all(_read_only(query) for query in sql)
        assert not (_imported_modules(source) & FORBIDDEN_IMPORTS)


def test_resolver_never_caches_standing() -> None:
    class Db:
        @contextmanager
        def transaction(self):
            yield None

    class Facts:
        reads = 0

        def gm_facts(self, *_args, **_kwargs):
            self.reads += 1
            return policy.GmFacts(1, False, self.reads, 0)

    facts = Facts()
    resolver = PrincipalResolver(Db(), facts)
    account = SessionData(1, "dm")
    campaign = ident.new_id(ident.CAMPAIGN)
    for expected in range(1, 101):
        assert resolver.gm(campaign, account=account).authz_revision == expected
    assert facts.reads == 100
    assert set(vars(resolver)) == {"_db", "_facts", "_clock"}
    cached = {"cache", "lru_cache", "cached_property"}
    for module in (principals, policy_facts, policy):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        assert not any(isinstance(n, ast.Attribute) and n.attr in cached for n in ast.walk(tree))
