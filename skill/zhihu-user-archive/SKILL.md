---
name: zhihu-user-archive
description: Archive a Zhihu user's public profile activity and authored content, including answers, articles/posts, pins/ideas, columns, questions, videos, activity feed, authored comments when exposed, and comments under collected content. Use when Codex is asked to crawl, export, back up, audit, search, or incrementally update a Zhihu user's historical public content or comments.
---

# Zhihu User Archive

Archive public Zhihu records with explicit coverage reporting. Never claim a complete archive when a category is unavailable, blocked, or count-mismatched.

## Workflow

1. Resolve the user to a canonical `https://www.zhihu.com/people/<url_token>` URL. Prefer a user-supplied profile URL. If only a nickname is known, verify the identity from a known answer/article or visible profile metadata before crawling.
2. State the requested scope. Interpret “该用户的所有评论” as comments authored by that user. Treat “其内容下的所有评论” as a separate, potentially much larger scope enabled with `--content-comments all`.
3. Collect only data visible to the current authorized session. Do not bypass login, CAPTCHA, rate limits, security checks, deleted-content controls, or private-account restrictions. Never read the user's normal browser cookie database or ask them to paste `z_c0` into chat.
4. Run `scripts/archive_zhihu_user.py` with the default `--auth-mode auto`. It starts publicly, uses an explicitly supplied cookie when present, and opens an isolated browser login only after an authentication or security challenge. Real Zhihu hosts reject `--delay` values below 1.5 seconds; local HTTP fixtures and `--self-test` may use zero delay. Keep retries low.
   Keep API requests on the script's stateful CookieJar so server `Set-Cookie` responses refresh short-lived session cookies automatically. Treat cookie names and values as opaque; never scrape, print, or special-case `BEC`.
5. Inspect `manifest.json`. Compare collected counts with visible profile counts when available. Report each requested category as `complete`, `partial`, `unavailable`, or `failed`.
6. Before reporting comment coverage, inspect `categories.content-comments.performance`: reconcile `mismatch_count` and `missing_sum`, then review request-amplification metrics such as `pages_requested`, `child_tree_traversals`, `child_trees_skipped_complete`, and `child_pages_requested`.
7. Deliver the output directory and summarize gaps. Never describe `comments_authored` as complete merely because the endpoint returned zero records; the public profile may not expose historical authored comments.

## Runtime setup

Use Python 3.10 or newer. Public and cookie-only modes use only the standard library. Browser authentication requires Playwright and a Chromium-family browser. On macOS, use macOS 14 or newer and install the Playwright-matched Chromium for the most reliable result, including on Apple Silicon:

```bash
python3 -m pip install playwright
python3 -m playwright install chromium
```

On Windows, install Playwright into the bundled Python runtime when normal `python` is only a Windows Store alias. The script can use installed Edge, so a separate Chromium download is optional:

```powershell
python -m pip install playwright
```

## Quick start

Windows PowerShell:

```powershell
python scripts/archive_zhihu_user.py `
  --user "https://www.zhihu.com/people/dashixiongmofan" `
  --output "D:\交易\萧铭meaning-知乎归档" `
  --auth-mode auto `
  --types answers,articles,pins,columns,activities,comments-authored `
  --expected-counts answers=351,articles=10,pins=399,columns=1 `
  --delay 2.0
```

macOS Terminal:

```bash
python3 scripts/archive_zhihu_user.py \
  --user "https://www.zhihu.com/people/dashixiongmofan" \
  --output "$HOME/Documents/dashixiongmofan-zhihu-archive" \
  --auth-mode auto \
  --types answers,articles,pins,columns,activities,comments-authored \
  --expected-counts answers=351,articles=10,pins=399,columns=1 \
  --delay 2.0
```

For a first archive that includes comments under the user's collected content, use the adaptive strategy explicitly (it is also the default):

```powershell
python scripts/archive_zhihu_user.py `
  --user "https://www.zhihu.com/people/<url_token>" `
  --output "D:\archive\<url_token>" `
  --auth-mode auto `
  --types answers,articles,pins `
  --content-comments all `
  --comment-strategy adaptive `
  --delay 2.0
```

`adaptive` first makes one score-ordered pass across parents, immediately stops a parent when the archived count reaches its `comment_count`, and sends only still-short parents through overlapping orders and legacy fallbacks. This is the normal first-archive choice.

`--auth-mode auto` opens a dedicated Chromium-family browser window after a `401`, `403`, or HTML security-verification response. Ask the user to sign in or complete the visible verification. The script resumes automatically and reuses that isolated profile on later runs. It does not read the normal browser profile or print cookies.

Pre-authenticate without starting an archive, or clear the dedicated session:

```text
python scripts/archive_zhihu_user.py --login-browser
python scripts/archive_zhihu_user.py --logout-browser
```

Use `python3` instead of `python` on macOS.

Keep the dedicated browser profile outside the workspace and deliverables. Its default location is `%LOCALAPPDATA%\Codex\zhihu-user-archive\browser-profile` on Windows, `~/Library/Application Support/Codex/zhihu-user-archive/browser-profile` on macOS, and `~/.local/share/codex/zhihu-user-archive/browser-profile` on Linux. The script prefers Playwright's matching Chromium when installed, then detects Chrome, Edge, or Chromium in system and per-user application directories. Use `--browser-executable <path>` for a nonstandard installation. Safari is not used.

Use an explicitly supplied Cookie file only as a compatibility fallback:

```text
python scripts/archive_zhihu_user.py `
  --user dashixiongmofan `
  --output "D:\交易\萧铭meaning-知乎归档" `
  --auth-mode cookie `
  --cookie-file "D:\secure\zhihu-cookie.txt" `
  --content-comments all `
  --refresh-existing-comments `
  --delay 2.5 `
  --resume
```

On macOS, use `python3`, POSIX paths, and `\` line continuations for cookie mode as shown in the macOS quick start.

Do not ask the user to paste cookie values into chat. Do not commit, quote, or include the cookie file or dedicated browser profile in deliverables. Delete either only if the user explicitly asks.

## Scope choices

- Use `answers,articles,pins,columns` for the user's authored core content.
- Add `activities` for the historical activity feed. Activities can duplicate authored content and include follows, votes, or other events.
- Add `comments-authored` for comments written by the user. This endpoint is not consistently exposed; treat failure or zero results as a coverage warning.
- Add `questions,zvideos` only when requested or when “all historical content” is intended.
- Use `--content-comments all` to crawl root and child comments under collected answers, articles, and pins. Expect this to be much larger than the authored-content crawl.
- Keep `--comment-strategy adaptive` for normal first archives and count-based completion with bounded overlap.
- Use `--comment-strategy single-pass` for a fast reconnaissance run that intentionally tries only the first score-ordered stage; expect unresolved count differences to remain partial.
- Use `--comment-strategy exhaustive` only when auditing endpoint overlap or investigating a persistent count mismatch; it traverses every configured order even after the visible count is reached and therefore makes more requests.
- Add `--refresh-existing-comments` with `--resume` when tracking newly posted comments under content whose comments were archived in an earlier run. Without it, previously visited parents are skipped to keep ordinary incremental runs fast.

## Output and verification

Read [references/schema.md](references/schema.md) when interpreting files or building downstream analysis.

- `records.jsonl` is the loss-minimizing normalized archive and includes the raw API object.
- `archive_state.sqlite3` is a disposable runtime database for deduplication, page checkpoints, and parent progress. `records.jsonl` remains canonical. If the database is absent, recreate its record index offline with `--output <directory> --rebuild-state-index`. By default this transactionally rebuilds only `record_index` and retains parent tasks, checkpoints, and stable-404 endpoint capability entries. If the database itself is suspect, preserve it for diagnosis and remove it from the active path before rebuilding a fresh database.
- `records.csv` is a flat searchable index.
- `markdown/<type>.md` contains readable full-text exports.
- `manifest.json` records request scope, endpoint status, counts, errors, and whether the run can be described as complete.

Use `--resume` to retain existing records and deduplicate by record type and ID. A resumed run may revisit earlier pages but does not duplicate records.
For ongoing comment tracking, combine `--resume --content-comments all --refresh-existing-comments` with a small `--max-items-per-type` so recent parents are revisited without re-crawling the full archive.
For a large category stopped by rate limiting, set a verified `--start-offsets answers=<archived count>` value to continue from the next API offset. Recheck counts afterward because new content can shift offsets.
After an interrupted run, use `--output <directory> --rebuild-exports` to regenerate CSV, Markdown, and manifest counts from the durable JSONL file without making network requests.
Before calling content-comment coverage complete, require `performance.mismatch_count == 0`; when it is nonzero, report `missing_sum` and inspect `top_mismatches` (up to 50 parents). A configured per-parent cap below the visible count is intentionally partial, never count-complete. Also compare page, child-traversal, complete-tree-skip, and child-page counts with the archive size so abnormal request amplification is visible. See [references/schema.md](references/schema.md) for field definitions.
List endpoints may omit or truncate `content`. After collecting IDs, run `--enrich-details --types answers,articles --resume` to fetch full answer and article bodies individually. Verify `manifest.json.detail_enrichment` and confirm archived raw objects contain non-empty `content` before calling the archive full-text complete.

## Failure handling

- In `auto` mode, on `401`, `403`, or an HTML verification page, open the isolated browser once and retry the failed request after the user completes login or verification. If it still fails, stop that category and record it as unavailable.
- Accept and retain `Set-Cookie` updates throughout a run. Permit at most one browser-auth refresh per failed request, but allow a later request to refresh again if a long archive outlives the saved session.
- On `429`, honor `Retry-After`, increase `--delay`, and resume later. Do not rotate identities or proxies to evade the limit.
- On security verification or CAPTCHA, pause and ask the user to complete it in their own browser. Do not automate the challenge.
- On deleted, private, or unavailable content, preserve the known metadata and mark the record or category partial.
- If endpoint shapes drift, inspect one saved error/body sample, patch the endpoint mapping or normalizer, run the offline self-test, and retry a small capped crawl before a full run.

## Testing

Run the deterministic offline test before relying on script changes:

```text
python scripts/archive_zhihu_user.py --self-test
python3 scripts/archive_zhihu_user.py --self-test
```

Then validate this Skill with the Skill Creator `quick_validate.py` script.
