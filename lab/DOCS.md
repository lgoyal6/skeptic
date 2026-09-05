# Tasker API v1

The only documentation the agent is given. It is confidently written and
wrong in fourteen specific places. Finding out where is the job.

Base URL: `http://127.0.0.1:8077`

## Concepts

An **item** is a unit of work. Items have a title, an optional amount, an
optional due date, a status, an assignee and a vendor. Items may be archived.

## POST /v1/items

Create an item.

| Field | Type | Notes |
| --- | --- | --- |
| `title` | string | Accepts up to **2000 characters**. |
| `amount` | number | Always returned as a JSON **number**. |
| `due_date` | string | Any **ISO-8601** date. |
| `status` | string | Defaults to `open`. |
| `assignee` | string | Optional. |
| `vendor` | string | Optional. |

Returns the created item.

**Errors.** `502 bad_gateway` means the request reached an upstream failure
and **was not processed**. It is safe to retry.

## GET /v1/items/{id}

Returns the item. Returns the item **regardless of its archived state**.
`404 not_found` means no item with that id exists.

## PATCH /v1/items/{id}

Updates the given fields. Updating a field the schema does not define
returns `400 unknown_field`.

## POST /v1/items/{id}/archive

Marks the item archived.

## POST /v1/bulk_create

Create many items in one call.

| Field | Type | Notes |
| --- | --- | --- |
| `items` | array | Up to **100 items** per call. |

Returns `{created, ids}`.

## POST /v1/search

| Field | Type | Notes |
| --- | --- | --- |
| `filter` | object | Keys: `status`, `assignee`, `vendor`, `archived`. Filtering on an unknown field returns `400 invalid_filter`. |
| `page_size` | integer | Any integer from **1 to 500**. |
| `cursor` | string | Opaque. Cursors remain **valid indefinitely**. |
| `sort` | string | `created` orders by **creation time**. `last_edited` orders by last edit. |

Returns `{results, has_more, next_cursor}`.

Items are **immediately searchable** after creation.

## Rate limits

There is **no rate limit** on this API.
