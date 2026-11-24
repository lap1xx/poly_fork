"""Polymarket Gamma keyset catalog acquisition and conservative reconciliation."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from queue import Empty, Queue
import threading
import time
import requests
from event_phase import classify_event_phase
from runtime_settings import RUNTIME_LIMIT_DEFAULTS

GAMMA = "https://gamma-api.polymarket.com"
PM_CATALOG_TIMEOUT = (3.0, 5.0)
PM_CATALOG_WORKERS = RUNTIME_LIMIT_DEFAULTS["gamma_workers"]
PM_CATALOG_PAGE_LIMIT = RUNTIME_LIMIT_DEFAULTS["gamma_page_limit"]
PM_CATALOG_MAX_PAGES = RUNTIME_LIMIT_DEFAULTS["gamma_max_pages_per_query"]
PM_CATALOG_MAX_RETRIES = RUNTIME_LIMIT_DEFAULTS["gamma_retries_per_page"]
PM_CATALOG_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
PM_CATALOG_RETRY_BACKOFF_SEC = 0.25
_PM_CATALOG_DIAGNOSTICS_LOCK = threading.Lock()
_PM_CATALOG_DIAGNOSTICS = {}

def _fetch_pm_catalog_query(
    tag, mode, page_limit=PM_CATALOG_PAGE_LIMIT, *,
    max_pages=PM_CATALOG_MAX_PAGES,
    max_retries=PM_CATALOG_MAX_RETRIES,
    retryable_statuses=PM_CATALOG_RETRYABLE_STATUSES,
    retry_backoff_sec=PM_CATALOG_RETRY_BACKOFF_SEC,
    timeout=PM_CATALOG_TIMEOUT,
    gamma_url=GAMMA,
):
    """Fetch one tag/mode stream to cursor exhaustion, or report it as partial."""
    events = []
    cursor = None
    seen_cursors = set()
    pages = 0
    raw_event_rows = 0
    invalid_event_rows = 0
    effective_page_limit = int(page_limit)
    last_http_status = None
    error = None
    cursor_exhausted = False
    page_cap_hit = False
    limit_downgraded = False

    while pages < max_pages:
        params = {
            "tag_slug": tag,
            "limit": effective_page_limit,
            "active": "true",
            "closed": "false",
        }
        if mode == "live":
            params["live"] = "true"
        if cursor is not None:
            params["after_cursor"] = cursor

        response = None
        fallback_limit = False
        for attempt in range(max_retries):
            try:
                response = requests.get(
                    gamma_url + "/events/keyset",
                    params=params,
                    timeout=timeout,
                )
                last_http_status = getattr(response, "status_code", None)
                response.raise_for_status()
                break
            except Exception as exc:
                exc_response = getattr(exc, "response", None)
                status = getattr(exc_response, "status_code", None)
                if status is None and response is not None:
                    status = getattr(response, "status_code", None)
                last_http_status = status or last_http_status
                if status == 400 and effective_page_limit > 100 and not limit_downgraded:
                    effective_page_limit = 100
                    limit_downgraded = True
                    fallback_limit = True
                    response = None
                    break
                retryable_status = status in retryable_statuses
                retryable_exception = isinstance(
                    exc, (requests.ConnectionError, requests.Timeout, OSError)
                )
                if (attempt + 1 < max_retries
                        and (retryable_status or retryable_exception)):
                    time.sleep(retry_backoff_sec * (2 ** attempt))
                    continue
                error = "HTTP_%s" % status if status else type(exc).__name__
                response = None
                break

        if fallback_limit:
            # Retry the same cursor with a smaller page size; do not count a page.
            continue
        if response is None:
            break

        try:
            payload = response.json()
        except Exception:
            error = "InvalidJSON"
            break
        if not isinstance(payload, dict):
            error = "InvalidResponseShape"
            break
        page_events = payload.get("events")
        if not isinstance(page_events, list):
            error = "InvalidEventsShape"
            break

        pages += 1
        raw_event_rows += len(page_events)
        valid_events = [event for event in page_events if isinstance(event, dict)]
        invalid_event_rows += len(page_events) - len(valid_events)
        events.extend(valid_events)

        if "next_cursor" not in payload:
            if len(page_events) < effective_page_limit:
                # Gamma currently omits this field on terminal short pages.
                cursor_exhausted = True
            else:
                # A full page without a cursor could be silently truncated.
                error = "MissingNextCursor"
            break

        next_cursor = payload.get("next_cursor")
        cursor_text = str(next_cursor).strip() if next_cursor is not None else ""
        if next_cursor is None or cursor_text in {"", "LTE="}:
            cursor_exhausted = True
            break
        if cursor_text == cursor or cursor_text in seen_cursors:
            error = "RepeatedCursor"
            break
        seen_cursors.add(cursor_text)
        cursor = cursor_text

    if pages >= max_pages and not cursor_exhausted and not error:
        page_cap_hit = True
        error = "PageCapReached"

    return {
        "tag": tag,
        "mode": mode,
        "pages": pages,
        "raw_event_rows": raw_event_rows,
        "invalid_event_rows": invalid_event_rows,
        "events": events,
        "cursor_exhausted": cursor_exhausted,
        "page_cap_hit": page_cap_hit,
        "effective_page_limit": effective_page_limit,
        "last_http_status": last_http_status,
        "error": error,
    }


def _fetch_pm_catalog_tag(
    tag, query_limit=PM_CATALOG_PAGE_LIMIT, *, query_fetch=None,
    query_options=None,
):
    fetch_query = query_fetch or _fetch_pm_catalog_query
    options = query_options or {}
    results = [
        fetch_query(tag, mode, query_limit, **options)
        for mode in ("live", "general")
    ]
    return tag, results


def get_pm_catalog_diagnostics():
    with _PM_CATALOG_DIAGNOSTICS_LOCK:
        snapshot = dict(_PM_CATALOG_DIAGNOSTICS)
    snapshot["error_counts"] = dict(snapshot.get("error_counts") or {})
    snapshot["failed_tags"] = list(snapshot.get("failed_tags") or [])
    snapshot["queries"] = [dict(query) for query in snapshot.get("queries") or []]
    return snapshot


def fetch_live_pm_markets(
    with_status=False, *, fetch_tag=None, tags=None, prefixes=None,
    worker_count=PM_CATALOG_WORKERS, page_limit=PM_CATALOG_PAGE_LIMIT,
    max_pages=PM_CATALOG_MAX_PAGES, max_retries=PM_CATALOG_MAX_RETRIES,
    retryable_statuses=PM_CATALOG_RETRYABLE_STATUSES,
    retry_backoff_sec=PM_CATALOG_RETRY_BACKOFF_SEC,
    timeout=PM_CATALOG_TIMEOUT, gamma_url=GAMMA,
):
    tags = list(tags or ("esports", "cs2", "dota-2", "league-of-legends",
                         "valorant", "tennis", "atp"))
    prefixes = tuple(prefixes or ("cs2-", "dota2-", "lol-", "valorant-",
                                  "atp-", "wta-", "itf-"))
    if fetch_tag is None:
        options = {
            "max_pages": max_pages, "max_retries": max_retries,
            "retryable_statuses": retryable_statuses,
            "retry_backoff_sec": retry_backoff_sec,
            "timeout": timeout, "gamma_url": gamma_url,
        }
        fetch_tag = lambda tag, limit: _fetch_pm_catalog_tag(
            tag, limit, query_options=options)
    started = time.monotonic()
    out = {}
    query_count = len(tags) * 2
    successful_queries = 0
    cursor_exhausted_queries = 0
    total_pages = 0
    page_cap_queries = 0
    invalid_event_rows = 0
    catalog_complete = True
    results_by_tag = {}
    error_counts = {}
    failed_tags = set()
    query_details = []

    # Keep the existing per-tag worker bound; each worker walks the live and
    # general keysets serially so cursor order is preserved within a stream.
    with ThreadPoolExecutor(
        max_workers=min(worker_count, len(tags)),
        thread_name_prefix="pm-catalog",
    ) as executor:
        futures = {
            executor.submit(fetch_tag, tag, page_limit): tag
            for tag in tags
        }
        for future in as_completed(futures):
            tag = futures[future]
            try:
                result_tag, results = future.result()
                results_by_tag[result_tag] = results
            except Exception as exc:
                error_type = type(exc).__name__
                results_by_tag[tag] = [
                    {
                        "tag": tag, "mode": mode, "pages": 0,
                        "raw_event_rows": 0, "invalid_event_rows": 0,
                        "events": [], "cursor_exhausted": False,
                        "page_cap_hit": False,
                        "effective_page_limit": page_limit,
                        "last_http_status": None, "error": error_type,
                    }
                    for mode in ("live", "general")
                ]

    for tag in tags:
        for result in results_by_tag.get(tag, []):
            pages = int(result.get("pages") or 0)
            total_pages += pages
            invalid_event_rows += int(result.get("invalid_event_rows") or 0)
            cursor_exhausted = bool(result.get("cursor_exhausted"))
            query_error = result.get("error")
            if cursor_exhausted:
                cursor_exhausted_queries += 1
            if result.get("page_cap_hit"):
                page_cap_queries += 1
            if not cursor_exhausted or query_error:
                catalog_complete = False
                failed_tags.add(tag)
                error_type = query_error or "CursorNotExhausted"
                error_counts[error_type] = error_counts.get(error_type, 0) + 1
            else:
                successful_queries += 1
            if result.get("invalid_event_rows"):
                catalog_complete = False

            query_details.append({
                "tag": tag,
                "mode": result.get("mode"),
                "pages": pages,
                "raw_event_rows": int(result.get("raw_event_rows") or 0),
                "cursor_exhausted": cursor_exhausted,
                "page_cap_hit": bool(result.get("page_cap_hit")),
                "effective_page_limit": result.get("effective_page_limit"),
                "last_http_status": result.get("last_http_status"),
                "error": query_error,
            })

            for event in result.get("events") or []:
                if not isinstance(event, dict):
                    catalog_complete = False
                    continue
                event_markets = event.get("markets") or []
                if not isinstance(event_markets, list):
                    catalog_complete = False
                    continue
                for market in event_markets:
                    if not isinstance(market, dict):
                        catalog_complete = False
                        continue
                    slug = market.get("slug", "")
                    if not slug or not slug.startswith(prefixes):
                        continue
                    if market.get("closed") is True:
                        continue
                    if market.get("acceptingOrders") is False:
                        continue
                    try:
                        outcomes = json.loads(market.get("outcomes", "[]"))
                        tokens = json.loads(market.get("clobTokenIds", "[]"))
                    except Exception:
                        catalog_complete = False
                        continue
                    if (
                        not isinstance(outcomes, list) or not isinstance(tokens, list)
                        or len(outcomes) != 2 or len(tokens) != 2
                        or any(not isinstance(value, str) or not value.strip()
                               for value in outcomes + tokens)
                    ):
                        catalog_complete = False
                        continue
                    start = (market.get("eventStartTime") or event.get("startTime") or "")
                    phase = classify_event_phase(market, event)
                    out[slug] = {
                        "slug": slug, "condition_id": market.get("conditionId"),
                        "q": market.get("question", ""),
                        "o": outcomes, "t": tokens, "start": start,
                        "phase": phase,
                        "_phase_classified": True,
                        "live": phase == "live",
                        "status": market.get("status") or event.get("status"),
                        "closed": market.get("closed", event.get("closed")),
                        "active": market.get("active", True),
                    }

    complete = query_count > 0 and successful_queries == query_count and catalog_complete
    diagnostics = {
        "query_count": query_count,
        "successful_queries": successful_queries,
        "failed_queries": query_count - successful_queries,
        "cursor_exhausted_queries": cursor_exhausted_queries,
        "total_pages": total_pages,
        "page_limit": page_limit,
        "max_pages_per_query": max_pages,
        "page_cap_queries": page_cap_queries,
        "invalid_event_rows": invalid_event_rows,
        "error_counts": error_counts,
        "failed_tags": sorted(failed_tags),
        "queries": query_details,
        "markets": len(out),
        "complete": complete,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
    }
    with _PM_CATALOG_DIAGNOSTICS_LOCK:
        _PM_CATALOG_DIAGNOSTICS.clear()
        _PM_CATALOG_DIAGNOSTICS.update(diagnostics)
    if diagnostics["failed_queries"] or not catalog_complete:
        errors = ", ".join(
            "%s=%s" % (name, count)
            for name, count in sorted(error_counts.items())
        ) or "invalid catalog data"
        print(
            "[!] Polymarket Gamma catalog: %d/%d query streams fully paginated; "
            "%d incomplete (%s), %d pages, %d markets in %.0f ms" % (
                successful_queries, query_count, diagnostics["failed_queries"],
                errors, total_pages, len(out), diagnostics["elapsed_ms"],
            ),
            flush=True,
        )
    return (out, complete) if with_status else out


class BackgroundCatalogRefresh:
    """Run one bounded catalog acquisition without blocking the scanner loop."""

    def __init__(self, fetch):
        self._fetch = fetch
        self._lock = threading.Lock()
        self._results = Queue(maxsize=1)
        self._running = False

    def submit(self):
        """Start one fetch; return False while a fetch/result is still pending."""
        with self._lock:
            if self._running or not self._results.empty():
                return False
            self._running = True
            threading.Thread(
                target=self._run,
                name="pm-catalog-refresh",
                daemon=True,
            ).start()
            return True

    def _run(self):
        try:
            result = (True, self._fetch())
        except Exception as exc:
            # Do not retain/log exception text that could contain request details.
            result = (False, type(exc).__name__)
        self._results.put(result)
        with self._lock:
            self._running = False

    def poll(self):
        """Return (success, value-or-error-type), or None without waiting."""
        try:
            return self._results.get_nowait()
        except Empty:
            return None


def reconcile_pm_market_map(current, fresh, refresh_complete):
    """Replace an old Gamma catalog only after a complete successful refresh."""
    current = current if isinstance(current, dict) else {}
    fresh = fresh if isinstance(fresh, dict) else {}
    added = len(set(fresh) - set(current))
    removed = len(set(current) - set(fresh)) if refresh_complete else 0
    merged = dict(fresh) if refresh_complete else {**current, **fresh}
    return merged, added, removed
