# SubCan Connectors & Evidence Retrieval Module

**Module Owner:** Chinmay  
**Target Package:** `backend/app/connectors/`  
**Test Suite:** `backend/tests/test_connectors.py`  
**Data Standards:** [docs/DATA_CONTRACT.md](file:///docs/DATA_CONTRACT.md)

---

## 1. Overview & Architecture

The SubCan Connectors module is responsible for the **DETECT** phase of the subscription cancellation workflow. It ingests subscriptions, retrieves historical usage/playback records, queries cancellation terms and consequences, and normalizes all data into canonical Pydantic models (`Subscription` and `Evidence`).

```
                    +------------------------------------+
                    |        ConnectorRegistry           |
                    +------------------------------------+
                                      |
         +----------------------------+----------------------------+
         |                            |                            |
         v                            v                            v
+------------------+         +------------------+         +------------------+
| CineVaultConnect |         | AudioCloudConnec |         | FitStreamConnect |
| (TC1: Inactive)  |         | (TC2: Data Loss) |         | (TC3: Pause Alt) |
+------------------+         +------------------+         +------------------+
         |                            |                            |
         v                            v                            v
  (Fixture/HTTP)               (Fixture/HTTP)               (Fixture/HTTP)
         \                            |                            /
          +---------------------------+---------------------------+
                                      |
                                      v
                      Canonical Models: Subscription & Evidence
```

---

## 2. Supported Connectors & Services

| Service ID | Service Display Name | Target Subscription ID | Test Scenario | Special Characteristics |
|---|---|---|---|---|
| `cinevault` | CineVault Mock Streaming | `sub_tc1_cinevault` | TC1 | Standard streaming service inactive for > 3 calendar months. |
| `audiocloud` | AudioCloud Pro Mock | `sub_tc2_audiocloud` | TC2 | Carries `data_deletion_warning=True` (loss of stems/playlists). |
| `fitstream` | FitStream Pass Mock | `sub_tc3_fitstream` | TC3 | Carries `pause_option_available=True` (90-day freeze alternative). |
| `moviemax` | MovieMax Mock | `sub_tc4_moviemax` | TC4 | Multi-subscription approval isolation (Target A). |
| `gamestream` | GameStream Mock | `sub_tc4_gamestream` | TC4 | Multi-subscription approval isolation (Target B). |
| `generic_fixture` | Generic Fixture Service | Any in `subscriptions.json` | Baseline | Fixture-backed reference adapter for all synthetic records. |

---

## 3. Core Interface: `BaseSubscriptionConnector`

Every connector inherits from `BaseSubscriptionConnector` in `backend/app/connectors/base.py` and implements the following public interface:

```python
class BaseSubscriptionConnector(ABC):
    @property
    def service_id(self) -> str: ...

    @property
    def service_name(self) -> str: ...

    def list_subscriptions(self) -> List[Subscription]: ...

    def get_subscription(self, subscription_id: str) -> Subscription: ...

    def get_usage_history(
        self, subscription_id: str, as_of: Optional[datetime] = None
    ) -> List[Evidence]: ...

    def get_cancellation_terms(
        self, subscription_id: str, as_of: Optional[datetime] = None
    ) -> List[Evidence]: ...

    def collect_evidence(
        self, subscription_id: str, as_of: Optional[datetime] = None
    ) -> List[Evidence]: ...

    def get_renewal_status(self, subscription_id: str) -> Dict[str, Any]: ...
```

---

## 4. Top-Level Facade & Agent Integration

The package entry point `backend/app/connectors/__init__.py` provides high-level functions consumed directly by the agent workflow:

### `fetch_subscriptions(service_name=None, registry=None) -> List[Subscription]`
Ingests all subscriptions across registered connectors or filters for a specific service.

### `get_subscription(subscription_id, registry=None) -> Subscription`
Retrieves a specific subscription entity, raising `SubscriptionNotFoundError` if missing.

### `collect_evidence(subscription_id, as_of=None, registry=None) -> List[Evidence]`
Gathers all usage logs, cancellation terms, and consequence disclosures for a subscription, deduplicating by `evidence_id`.

### `load_connector_data(target_subscription_ids=None, registry=None, as_of=None) -> Tuple[List[Subscription], Dict[str, List[Evidence]]]`
Drop-in data loader producing `(subscriptions, evidence_map)` directly consumable by Loki's `agent.initialize_orchestration_run(...)`.

---

## 5. Usage History & Evidence Semantics

1. **No Date Fabrication:** If usage history is unavailable, `Subscription.last_used_at` remains `None`. The connector never invents synthetic dates or interprets missing logs as inactivity.
2. **Timezone Awareness:** All `Evidence.captured_at` timestamps are strictly timezone-aware (ISO 8601 with UTC offset). Naive datetimes are assigned UTC.
3. **Retrieval Time vs Event Time:** The connector explicitly distinguishes between `captured_at` (when the connector retrieved the log) and the event occurrence timestamp preserved in `raw_reference` and `excerpt`.
4. **Deduplication:** Multiple calls or combined sources deduplicate evidence records by `evidence_id` to prevent inflated uncertainty scores in downstream rules.
5. **Provenance:** Every `Evidence` instance includes `source_url`, `captured_at`, `confidence`, and `raw_reference` metadata.

---

## 6. Error Handling Hierarchy

All connector errors inherit from `ConnectorError` in `backend/app/connectors/exceptions.py`:

```
ConnectorError
├── UnsupportedServiceError (Requested service not registered)
├── SubscriptionNotFoundError (Subscription ID does not exist)
├── ConnectorConnectionError (Network host unreachable)
├── ConnectorTimeoutError (Request timed out)
├── ConnectorHTTPError (HTTP 4xx / 5xx error responses)
│   └── ConnectorAuthError (HTTP 401 / 403 authorization failed)
├── ConnectorResponseError (Malformed or non-JSON response payload)
└── EvidenceExtractionError (Validation or timestamp normalization failure)
```

**Safety Invariant:** A connector error is **never** silently converted into a successful retrieval or an empty list.

---

## 7. Configuration & Authentication

### Environment Variables (`.env`)
```bash
# Optional base URL for mock HTTP streaming endpoints (defaults to https://mock.streaming.test)
MOCK_SITE_BASE_URL=https://mock.streaming.test

# Request timeout in seconds
MOCK_SITE_TIMEOUT_SECONDS=5.0

# Optional API token for secured mock endpoints
MOCK_SITE_API_KEY=mock_secret_key_placeholder
```

### Authentication Behavior
- In fixture mode (default), zero network calls or credentials are required.
- In HTTP mode, `HttpMockSubscriptionConnector` passes `Authorization: Bearer <key>` when configured.
- Secret Hygiene: Passwords, tokens, and cookies are never printed in logs or included in error messages.

---

## 8. How to Add a New Connector

1. Subclass `BaseSubscriptionConnector` or `ServiceConnectorAdapter`:
   ```python
   from backend.app.connectors.services import ServiceConnectorAdapter

   class NewStreamConnector(ServiceConnectorAdapter):
       def __init__(self, **kwargs):
           super().__init__(
               service_id="newstream",
               service_name="NewStream Mock",
               supported_aliases=["newstream", "NewStream"],
               **kwargs,
           )
   ```

2. Register the connector with the default registry:
   ```python
   from backend.app.connectors import get_default_registry

   get_default_registry().register(NewStreamConnector())
   ```

3. Query subscriptions and evidence using standard package functions:
   ```python
   from backend.app.connectors import fetch_subscriptions, collect_evidence

   subs = fetch_subscriptions(service_name="newstream")
   evidence = collect_evidence(subs[0].subscription_id)
   ```

---

## 9. Running Tests

Run connector unit and integration tests:
```bash
pytest -v backend/tests/test_connectors.py
```

Run the complete SubCan test suite:
```bash
pytest -v
```

All 36 connector tests and 165 total repository tests execute deterministically in seconds without live external network calls.
