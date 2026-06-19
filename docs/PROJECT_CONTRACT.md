# Project Contract

This file records the shared contract for the current phase of the project.
It is the source of truth for configuration naming and raw WebSocket payloads.

## Deployment Shape

- `plugin` and `server` are deployed on the same host.
- `client` runs on the classroom computer.
- The system is currently designed for a single client instance.
- The single client serves the whole class.

## Configuration Rules

- Shared root config lives in `configs/app.toml`.
- The checked-in template lives in `configs/app.example.toml`.
- `INTERNAL_TOKEN` is the only authentication token name used across the project.
- `Socket.IO` is not used. All internal communication uses raw WebSocket.

## Shared Endpoints

- Client endpoint: `/ws/client`
- Plugin endpoint: `/ws/plugin`

The final URLs are derived from the root server host and port unless explicitly overridden.

## Message Identity Rules

- `db_id` is the persistent message identity inside SQLite.
- Client read acknowledgement must use `db_id`.
- `short_id` is a temporary mapping used only by the QQ side after `/query` or `/cx`.
- `short_id` expires after `short_id_ttl_seconds`, default `300`.

## Envelope Shape

All raw WebSocket messages use the same JSON envelope:

```json
{
  "type": "message_type",
  "request_id": "optional-request-id",
  "auth_token": "optional-internal-token",
  "data": {}
}
```

## Planned Message Types

### Shared

- `auth`
- `auth_ok`
- `auth_error`
- `heartbeat`
- `error`

### Plugin -> Server

- `new_message`
- `query_unread`
- `recall_message`
- `resend_message`

### Client -> Server

- `mark_read`
- `status_update`

### Server -> Client

- `pending_messages`
- `recall_result`
- `resend_result`
- `status_snapshot`

### Server -> Plugin

- `query_unread_result`
- `read_receipt`
- `new_message_stored`

## Payload Notes

### `new_message`

- carries sender identity, sender display name, message text, priority, timestamp
- priority values: `normal`, `urgent`

### `mark_read`

- carries `db_id`
- used by client after the student confirms the message

### `status_update`

- carries online state and current mode
- mode values: `normal`, `exam`

### `query_unread_result`

- returns unread messages for the requesting sender only
- each result row contains `db_id`, `short_id`, `content_preview`, `msg_type`, `timestamp`

## Persistence Notes

- `EXAM_MODE` must be persisted in SQLite so server restarts do not lose state.
- Message status is state-based, not delete-based:
  - `unread`
  - `read`
  - `recalled`

