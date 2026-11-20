"""PostgreSQL wakeup channels for table reads.

Payloads are opaque IDs. Subscribers re-read authorization and current slot
state because delivery is only a hint and may be coalesced or missed.
"""

from typing import Final

SESSION_WAKEUP_CHANNEL: Final = "table_session"
CAMPAIGN_WAKEUP_CHANNEL: Final = "table_campaign"
CREDENTIAL_WAKEUP_CHANNEL: Final = "table_credential"
