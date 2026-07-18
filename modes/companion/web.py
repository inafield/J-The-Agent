"""Web search and page fetch for Companion (stdlib first, optional API keys)."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any

from modes.companion.settings import CompanionSettings, WebSearchProvider

_USER_AGENT = "J-the-Agent/0.4 (+https://github.com/inafield/J_The_Agent)"


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        text = data.strip()
        if text:
            self._chunks.append(text)

    def text(self) -> str:
        return "\n".join(self._chunks)


class _DDGParser(HTMLParser):
    """Extract result titles/links/snippets from DuckDuckGo HTML lite."""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._in_result = False
        self._result_depth = 0
        self._in_title = False
        self._in_snippet = False
        self._snippet_tag = ""
        self._href = ""
        self._title: list[str] = []
        self._snippet: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        classes = (attrs_dict.get("class") or "").split()
        if self._in_result and tag == "div":
            self._result_depth += 1
        elif tag == "div" and "result" in classes:
            self._in_result = True
            self._result_depth = 1
            self._href = ""
            self._title = []
            self._snippet = []
        if not self._in_result:
            return
        if tag == "a" and "result__a" in classes:
            self._in_title = True
            self._href = attrs_dict.get("href") or ""
        if "result__snippet" in classes or "result-snippet" in classes:
            self._in_snippet = True
            self._snippet_tag = tag

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_title:
            self._in_title = False
        if tag == self._snippet_tag and self._in_snippet:
            self._in_snippet = False
            self._snippet_tag = ""
        if not self._in_result or tag != "div":
            return
        self._result_depth -= 1
        if self._result_depth == 0 and self._title:
            self.results.append(
                {
                    "title": " ".join(self._title).strip(),
                    "url": self._unwrap_ddg(self._href),
                    "snippet": " ".join(self._snippet).strip(),
                }
            )
            self._in_result = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title.append(data.strip())
        elif self._in_snippet:
            self._snippet.append(data.strip())

    @staticmethod
    def _unwrap_ddg(url: str) -> str:
        if "uddg=" in url:
            parsed = urllib.parse.urlparse(url)
            query = urllib.parse.parse_qs(parsed.query)
            if "uddg" in query:
                return urllib.parse.unquote(query["uddg"][0])
        return url


def _request(
    url: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 20,
    max_bytes: int = 2_000_000,
) -> str:
    request = urllib.request.Request(
        url,
        data=data,
        headers={"User-Agent": _USER_AGENT, **(headers or {})},
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        payload = response.read(max_bytes + 1)
        if len(payload) > max_bytes:
            raise OSError(f"Response exceeds {max_bytes} bytes")
        return payload.decode("utf-8", errors="replace")


def search_web(settings: CompanionSettings, query: str, *, max_results: int = 5) -> dict[str, Any]:
    """Search using the configured provider. Returns a structured payload."""

    query = query.strip()
    if not query:
        return {"ok": False, "error": "empty query", "results": []}
    max_results = min(max(max_results, 1), 10)
    provider = settings.web_provider

    if provider is WebSearchProvider.NONE:
        return {
            "ok": False,
            "error": "Web search disabled. Run: ja setup",
            "results": [],
        }
    if provider is WebSearchProvider.DUCKDUCKGO:
        return _search_duckduckgo(query, max_results=max_results)
    if provider is WebSearchProvider.BRAVE:
        return _search_brave(settings, query, max_results=max_results)
    if provider is WebSearchProvider.SEARXNG:
        return _search_searxng(settings, query, max_results=max_results)
    if provider is WebSearchProvider.TAVILY:
        return _search_tavily(settings, query, max_results=max_results)
    return {"ok": False, "error": f"Unknown provider {provider}", "results": []}


def fetch_url(
    url: str,
    *,
    max_chars: int = 12_000,
    allowed_local_ports: list[int] | None = None,
) -> dict[str, Any]:
    from modes.companion.url_safety import check_fetch_url

    url = url.strip()
    check = check_fetch_url(url, allowed_local_ports=allowed_local_ports)
    if not check.ok:
        error = check.error or "URL blocked"
        if check.suggestion:
            error = f"{error} {check.suggestion}"
        return {
            "ok": False,
            "error": error,
            "url": url,
            "is_local": check.is_local,
            "suggestion": check.suggestion,
        }
    try:
        html = _request(url, timeout=25)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "error": str(exc), "url": url}
    parser = _TextExtractor()
    parser.feed(html)
    text = re.sub(r"\n{3,}", "\n\n", parser.text()).strip()
    if len(text) > max_chars:
        text = text[:max_chars] + "\n… [truncated]"
    return {
        "ok": True,
        "url": url,
        "text": text or "(no readable text)",
        "is_local": check.is_local,
        "whitelisted": check.whitelisted,
    }


def _search_duckduckgo(query: str, *, max_results: int) -> dict[str, Any]:
    body = urllib.parse.urlencode({"q": query}).encode()
    try:
        html = _request(
            "https://html.duckduckgo.com/html/",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "error": f"DuckDuckGo request failed: {exc}", "results": []}
    parser = _DDGParser()
    parser.feed(html)
    results = [item for item in parser.results if item.get("title")][:max_results]
    if not results:
        # Fallback: Instant Answer API (facts / abstracts only).
        try:
            raw = _request(
                "https://api.duckduckgo.com/?"
                + urllib.parse.urlencode({"q": query, "format": "json", "no_html": 1})
            )
            data = json.loads(raw)
            abstract = (data.get("AbstractText") or "").strip()
            if abstract:
                results = [
                    {
                        "title": data.get("Heading") or query,
                        "url": data.get("AbstractURL") or "",
                        "snippet": abstract,
                    }
                ]
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            pass
    return {"ok": True, "provider": "duckduckgo", "results": results}


def _search_brave(settings: CompanionSettings, query: str, *, max_results: int) -> dict[str, Any]:
    key = settings.web_api_key.get_secret_value() if settings.web_api_key else ""
    if not key:
        return {"ok": False, "error": "Brave API key missing. Run: ja setup", "results": []}
    url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode(
        {"q": query, "count": max_results}
    )
    try:
        raw = _request(url, headers={"X-Subscription-Token": key, "Accept": "application/json"})
        data = json.loads(raw)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"Brave search failed: {exc}", "results": []}
    results = [
        {
            "title": item.get("title") or "",
            "url": item.get("url") or "",
            "snippet": item.get("description") or "",
        }
        for item in (data.get("web") or {}).get("results") or []
    ][:max_results]
    return {"ok": True, "provider": "brave", "results": results}


def _search_searxng(settings: CompanionSettings, query: str, *, max_results: int) -> dict[str, Any]:
    base = (settings.web_base_url or "").rstrip("/")
    if not base:
        return {
            "ok": False,
            "error": "SearXNG base URL missing. Run: ja setup",
            "results": [],
        }
    url = f"{base}/search?" + urllib.parse.urlencode(
        {"q": query, "format": "json", "language": "auto"}
    )
    try:
        raw = _request(url)
        data = json.loads(raw)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"SearXNG search failed: {exc}", "results": []}
    results = [
        {
            "title": item.get("title") or "",
            "url": item.get("url") or "",
            "snippet": item.get("content") or "",
        }
        for item in data.get("results") or []
    ][:max_results]
    return {"ok": True, "provider": "searxng", "results": results}


def _search_tavily(settings: CompanionSettings, query: str, *, max_results: int) -> dict[str, Any]:
    key = settings.web_api_key.get_secret_value() if settings.web_api_key else ""
    if not key:
        return {"ok": False, "error": "Tavily API key missing. Run: ja setup", "results": []}
    payload = json.dumps(
        {"api_key": key, "query": query, "max_results": max_results}
    ).encode()
    try:
        raw = _request(
            "https://api.tavily.com/search",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        data = json.loads(raw)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"Tavily search failed: {exc}", "results": []}
    results = [
        {
            "title": item.get("title") or "",
            "url": item.get("url") or "",
            "snippet": item.get("content") or "",
        }
        for item in data.get("results") or []
    ][:max_results]
    return {"ok": True, "provider": "tavily", "results": results}
