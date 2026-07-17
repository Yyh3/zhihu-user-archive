# Archive schema and coverage

## Files

### `records.jsonl`

Each line is one UTF-8 JSON object:

- `archive_type`: `answer`, `article`, `pin`, `column`, `activity`, `comment_authored`, `comment_received`, `question`, or `zvideo`.
- `id`: stable Zhihu object ID when exposed; otherwise a deterministic hash.
- `title`, `url`, `text`, `excerpt`: normalized readable fields.
- `created_time`, `updated_time`: Unix timestamps when exposed.
- `author_name`, `author_token`: author metadata.
- `comment_count`, `voteup_count`: numeric counters when exposed.
- `parent_type`, `parent_id`: set for comments or nested records.
- `raw`: the original API object for loss-minimizing preservation.

### `records.csv`

A flat UTF-8 BOM CSV containing the normalized fields above except `raw`. Newlines are preserved inside quoted CSV cells.

### `markdown/<type>.md`

Readable documents grouped by archive type. HTML is converted to plain text; the JSONL retains original markup inside `raw`.

### `manifest.json`

- `target`: requested user and resolved URL token.
- `requested_types`: requested top-level categories.
- `content_comments`: `none` or `all`.
- `authentication`: selected authentication mode and whether isolated-browser fallback was available; it never contains cookie values.
- `categories`: per-category request count, unique record count, HTTP/error details, and status.
- `total_unique_records`: deduplicated total.
- `complete`: true only when every requested category completed without a coverage warning.

## Coverage meanings

- `complete`: pagination reached the endpoint's end and no known count conflict exists.
- `partial`: some records were collected, but pagination, child comments, or visible-count comparison indicates a gap.
- `unavailable`: authentication, security verification, privacy, endpoint removal, or unsupported access prevented collection.
- `failed`: an unexpected transport or parsing error prevented collection.

Authored comments deserve special caution: Zhihu may not expose a public historical-comments tab or API for a profile. A zero-record response proves only that the tested endpoint returned zero records, not that the user never commented.

## Endpoint families

The script uses currently observed `/api/v4` endpoint families and follows server-provided pagination links. These are implementation details rather than a stable public API contract. Keep endpoint mappings in the script centralized and treat changes as expected drift.
