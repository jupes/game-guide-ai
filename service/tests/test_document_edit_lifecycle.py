"""The edit worker must have enough attempt budget before it constructs a model client."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, Mock

from service import document_edits
from service.workbench_contracts import ErrorCode, ErrorInfo


def test_insufficient_remaining_budget_never_builds_a_provider_client():
    now = datetime(2026, 11, 19, tzinfo=UTC)
    admission = Mock(spec=document_edits.EditAdmission)
    admission.row = Mock(attempt_deadline=now + timedelta(milliseconds=1))
    stores = Mock(spec=document_edits.EditStores)
    stores.edits = Mock()
    stores.edits.fence.return_value.cancel_requested = False
    factory = Mock()

    outcome = document_edits.execute_edit(MagicMock(), stores, admission, factory, lambda: now)

    assert isinstance(outcome, ErrorInfo) and outcome.code is ErrorCode.PROVIDER_TIMEOUT
    stores.edits.fence.assert_called_once()
    factory.client_for.assert_not_called()
