"""Fetching a page from the internet, and the guards that make that safe to offer.

A local agent with a filesystem tool and a shell is already powerful; handing it a
URL adds a way for data to leave. Everything here is built around that one
sentence:

* **It cannot be pointed inside.** `assert_public_url` refuses anything that
  resolves to a private, loopback, link-local or reserved address, and it runs again
  on every redirect — a public URL that 302s to `127.0.0.1:8765` is how a fetch tool
  reaches the app's own API, or a cloud metadata endpoint, without ever being asked
  to. Redirects are followed by hand for exactly this reason; `follow_redirects=True`
  would make the check advisory.
* **It cannot be asked for a file.** Only `http` and `https` are schemes here, so
  `file:///etc/passwd` and `data:` are not URLs this tool knows about.
* **It cannot be used to exfiltrate quietly.** The call needs the user's approval
  unless they have trusted the tool for the project, and the approval prompt shows
  the URL — which is the part that would carry anything out. That is also why it is
  `approval="ask"` rather than `never`: reading a page is harmless, *sending a
  request* is not, and the same request through `run_shell` is already gated. A
  search is gated for the same reason: the query is what leaves.
* **It cannot be made to return a gigabyte.** The body is read up to a limit, only
  text-ish content types are accepted, and what comes back says how much was left
  out.

Search is a **scrape**, and says so rather than pretending otherwise: it posts the
query to DuckDuckGo's no-JavaScript HTML endpoint and reads the results out of the
markup, because there is no key-free search API and the alternative was a key the
user has to go and get. Nothing here is a contract — the endpoint can change its
markup, rate-limit, or stop answering — so a search that cannot be read is reported
as a failure with that explanation rather than as "no results", which is the one
outcome that would be worse than an error.

Not covered, and worth knowing: the address is validated and then the connection is
made by hostname, so a name server that answers differently between the two lookups
could still land on a private address. Pinning the validated address would close
that; it also breaks virtual hosting and TLS verification, which is why it is not
done here. This is a single-user tool on the user's own machine, not a service
fetching on behalf of strangers.
"""

from __future__ import annotations

import html as html_module
import ipaddress
import socket
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import ClassVar
from urllib.parse import parse_qs, urlsplit

import httpx

__all__ = [
    "FetchError",
    "FetchedPage",
    "SearchHit",
    "SearchResults",
    "assert_public_url",
    "choose_provider",
    "fetch_page",
    "html_to_text",
    "search",
]

# Redirects are followed one at a time so each hop can be validated. Five is more
# than any documentation site needs and few enough that a loop cannot run long.
_MAX_REDIRECTS = 5
# The connection and the read, separately: a slow site should not hold a turn open
# forever, and the read is bounded again by _MAX_BYTES.
_TIMEOUT = httpx.Timeout(10.0, read=15.0)
_MAX_BYTES = 2 * 1024 * 1024
_USER_AGENT = "Surtitle/1.0 (+https://github.com/mjoconr/Surtitle)"

# DuckDuckGo's no-JavaScript endpoint, used when there is no key. The ordinary site
# needs a browser to render anything, so this is the one that answers with results
# in the markup.
_SEARCH_URL = "https://html.duckduckgo.com/html/"
# Tavily, used when there is one. A real search API: JSON in, JSON out, no markup to
# parse and nobody to get rate-limited by. Optional, because it needs a key.
_TAVILY_URL = "https://api.tavily.com/search"
# What Tavily calls a "basic" search: one credit per call, and it is the depth whose
# results are the snippets this tool returns. `advanced` costs two and returns more
# content per result than a snippet field should hold.
_TAVILY_DEPTH = "basic"
# Enough to choose from, few enough to read. The agent can search again.
_MAX_RESULTS = 6
_MAX_QUERY_CHARS = 400
# A snippet is a sentence or two; the result list is not the page.
_MAX_SNIPPET_CHARS = 400
# What the bot challenge says, lowercased. Checked as well as the status, because
# relying on `202` alone would break the day they change it and take the real reason
# with it.
_CHALLENGE_MARKER = "complete the following challenge"

# Content types worth turning into text. A PDF or an image is not fetched and
# guessed at: the tool says what it found and the agent asks for something else.
_TEXTUAL = (
    "text/",
    "application/json",
    "application/xml",
    "application/xhtml",
    "application/rss+xml",
    "application/atom+xml",
)


class FetchError(RuntimeError):
    """A fetch that did not happen, with a reason the model can act on."""


@dataclass(slots=True)
class FetchedPage:
    """What a successful fetch produced."""

    url: str
    status: int
    content_type: str
    text: str
    truncated: bool = False


@dataclass(slots=True)
class SearchHit:
    """One result: what it is called, where it goes, and what it says."""

    title: str
    url: str
    snippet: str = ""


@dataclass(slots=True)
class SearchResults:
    """What a search found, and enough about the search to judge it."""

    query: str
    hits: list[SearchHit] = field(default_factory=list)
    # Which provider answered: "tavily" when a key is configured, otherwise
    # "duckduckgo". Reported rather than inferred, because "no results" means
    # different things depending on who was asked.
    provider: str = "duckduckgo"
    # The endpoint answered with a page carrying no result links at all. Carried
    # rather than raised so the tool can say "the markup moved" instead of "nothing
    # matched", which are very different things to be told. Testing never produced a
    # genuine empty-result page from this endpoint — a nonsense query still came back
    # with fuzzy matches — so this is deliberately not being guessed at as "no
    # results", which is the one claim that would be worse than an error.
    unreadable: bool = False
    # The endpoint answered with its bot challenge rather than results. A third
    # outcome again, and the one that is most often the truth: this is a keyless
    # scrape, so DuckDuckGo rate-limits it. Measured from one machine: the first
    # query returned ten results, the next four got the challenge — as `202`, not an
    # error status — and a query several minutes later worked again. The agent is
    # told which it was, because "search is blocked" and "the web has nothing" lead
    # to opposite next moves.
    blocked: bool = False


def assert_public_url(url: str) -> str:
    """Return the URL if it is one this tool may request, or raise.

    The gate is the *resolved address*, not the name: ``localhost``,
    ``metadata.google.internal`` and a domain someone points at ``10.0.0.1`` are all
    refused for the same reason, which a name-based blocklist would miss.
    """
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        raise FetchError(f"only http and https URLs can be fetched, not {parts.scheme or 'that'!r}")
    if not parts.hostname:
        raise FetchError("that URL has no host")
    if parts.username or parts.password:
        # Credentials in a URL are a way to smuggle a secret into a request that
        # looks ordinary in the approval prompt.
        raise FetchError("URLs with a username or password are not fetched")

    try:
        addresses = {
            info[4][0]
            for info in socket.getaddrinfo(parts.hostname, parts.port or _default_port(parts))
        }
    except socket.gaierror as exc:
        raise FetchError(f"could not resolve {parts.hostname}: {exc}") from exc
    if not addresses:
        raise FetchError(f"{parts.hostname} did not resolve to an address")

    for raw in addresses:
        address = ipaddress.ip_address(raw)
        mapped = getattr(address, "ipv4_mapped", None)
        if mapped is not None:
            address = mapped
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            raise FetchError(
                f"{parts.hostname} resolves to {raw}, which is not a public address; "
                "this tool does not fetch from the local network"
            )
    return url.strip()


def _default_port(parts) -> int:
    return 443 if parts.scheme == "https" else 80


class _TextExtractor(HTMLParser):
    """The readable text of a page, without the markup around it.

    Deliberately a small, dependency-free parser rather than a readability library:
    the model needs the words and the structure, and a page that parses badly should
    cost a little noise rather than a new dependency.
    """

    _SKIP: ClassVar[set[str]] = {"script", "style", "noscript", "template", "svg", "head"}
    _BREAK: ClassVar[set[str]] = {
        "p",
        "div",
        "br",
        "li",
        "tr",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "section",
        "article",
        "header",
        "footer",
        "pre",
        "blockquote",
        "table",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._pieces: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self._SKIP:
            self._skipping += 1
        elif tag in self._BREAK:
            self._pieces.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skipping:
            self._skipping -= 1
        elif tag in self._BREAK:
            self._pieces.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self._pieces.append(data)

    def text(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self._pieces).splitlines()]
        return "\n".join(line for line in lines if line)


def html_to_text(markup: str) -> str:
    """The words on a page, with runs of whitespace collapsed."""
    parser = _TextExtractor()
    parser.feed(markup)
    parser.close()
    return parser.text()


def fetch_page(url: str, *, transport: httpx.BaseTransport | None = None) -> FetchedPage:
    """Fetch one page, following redirects by hand so each hop can be checked.

    ``transport`` exists so tests can drive this without a network; every guard
    below runs the same either way, which is the point of putting them here rather
    than in the tool.
    """
    response = _get(url, transport=transport, accept="text/html,text/plain,*/*;q=0.5")
    text = response.body
    if "html" in response.content_type or text.lstrip()[:1] == "<":
        text = html_to_text(text)
    return FetchedPage(
        url=response.url,
        status=response.status,
        content_type=response.content_type or "unknown",
        text=text.strip(),
        truncated=response.truncated,
    )


@dataclass(slots=True)
class _Response:
    """One response, before the caller decides whether it wants text or markup."""

    url: str
    status: int
    content_type: str
    body: str
    truncated: bool = False


def _get(url: str, *, transport: httpx.BaseTransport | None, accept: str, params=None) -> _Response:
    """GET one URL and return its decoded body, checking every hop on the way.

    The single place the redirect loop lives. Search needs the markup where a fetch
    wants the text, and neither should get its own copy of the address checks —
    a guard that exists twice is a guard that is wrong once.
    """
    current = assert_public_url(url)
    with httpx.Client(
        timeout=_TIMEOUT,
        follow_redirects=False,
        headers={"User-Agent": _USER_AGENT, "Accept": accept},
        transport=transport,
    ) as client:
        for _ in range(_MAX_REDIRECTS + 1):
            response = client.get(current, params=params)
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise FetchError(f"{current} redirected without saying where")
                current = assert_public_url(str(httpx.URL(current).join(location)))
                continue

            if response.status_code >= 400:
                raise FetchError(f"{current} answered {response.status_code}")

            content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if content_type and not any(content_type.startswith(kind) for kind in _TEXTUAL):
                raise FetchError(
                    f"{current} is {content_type}, which is not text; this tool reads "
                    "pages and documents, not binaries"
                )

            body = response.content[:_MAX_BYTES]
            truncated = len(response.content) > _MAX_BYTES
            return _Response(
                url=str(response.url),
                status=response.status_code,
                content_type=content_type,
                body=body.decode(response.encoding or "utf-8", errors="replace"),
                truncated=truncated,
            )
    raise FetchError(f"{url} redirected more than {_MAX_REDIRECTS} times")


@dataclass(slots=True)
class _Response:
    """One response, before the caller decides whether it wants text or markup."""

    url: str
    status: int
    content_type: str
    body: str
    truncated: bool = False


@dataclass(slots=True)
class _Response:
    """One response, before the caller decides whether it wants text or markup."""

    url: str
    status: int
    content_type: str
    body: str
    truncated: bool = False


class _ResultParser(HTMLParser):
    """DuckDuckGo's result list, read out of the markup.

    Keyed on the classes the endpoint uses — ``result__a`` for a result's link and
    ``result__snippet`` for its text. That is the fragile part of a scrape and it is
    why ``SearchResults.unreadable`` exists: markup this parser does not recognise
    must not be reported as "no results".
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hits: list[SearchHit] = []
        self._in_title = False
        self._in_snippet = False
        self._saw_any_result = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag != "a":
            return
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        href = attributes.get("href") or ""
        if "result__a" in classes:
            self._saw_any_result = True
            url = _unwrap_result_url(href)
            self.hits.append(SearchHit(title="", url=url))
            self._in_title = True
        elif "result__snippet" in classes:
            self._saw_any_result = True
            if self.hits:
                self._in_snippet = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._in_title = False
            self._in_snippet = False

    def handle_data(self, data: str) -> None:
        if not self.hits:
            return
        text = " ".join(data.split())
        if not text:
            return
        if self._in_title:
            hit = self.hits[-1]
            hit.title = (hit.title + " " + text).strip() if hit.title else text
        elif self._in_snippet:
            hit = self.hits[-1]
            hit.snippet = (hit.snippet + " " + text).strip() if hit.snippet else text

    @property
    def saw_any_result(self) -> bool:
        """Whether the page was a result list at all, readable or not."""
        return self._saw_any_result


def _unwrap_result_url(href: str) -> str:
    """The address a result actually points at.

    DuckDuckGo wraps every link in a redirect of its own —
    ``//duckduckgo.com/l/?uddg=<urlencoded>`` — so the href in the markup is not
    where the result goes. Unwrapping it here means the model is given the address
    it will actually fetch, and a redirect parameter cannot carry it somewhere the
    fetch tool would refuse without that being visible.
    """
    candidate = html_module.unescape(href.strip())
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    if "uddg=" not in candidate:
        return candidate
    try:
        query = parse_qs(urlsplit(candidate).query)
    except ValueError:  # pragma: no cover - a URL urlsplit refuses
        return candidate
    unwrapped = query.get("uddg", [])
    return unwrapped[0] if unwrapped else candidate


# The providers a user may choose between, and the default. "automatic" is the
# useful default: the best one this machine can actually use.
SEARCH_PROVIDERS = ("automatic", "duckduckgo", "tavily")


def choose_provider(preference: str | None, api_key: str | None) -> str:
    """Which provider a search will actually use, or raise if it cannot be made.

    Choosing Tavily without a key is a configuration mistake, and the one thing not
    to do about it is fall back silently: the user asked for the provider they pay
    for and would be getting a scrape, with no way to tell from the results.
    """
    wanted = (preference or "automatic").strip().lower()
    if wanted == "duckduckgo":
        return "duckduckgo"
    if wanted == "tavily":
        if not api_key:
            raise FetchError(
                "Web search is set to Tavily, which needs an API key, and none is "
                "set. Add one under Settings → API keys, or set the search provider "
                "back to automatic."
            )
        return "tavily"
    return "tavily" if api_key else "duckduckgo"


def search(
    query: str,
    *,
    limit: int = _MAX_RESULTS,
    transport: httpx.BaseTransport | None = None,
    api_key: str | None = None,
    provider: str | None = None,
) -> SearchResults:
    """Search the web, with whichever provider the caller has settled on.

    Tavily when there is a key and the setting allows it, the keyless DuckDuckGo
    scrape otherwise. Both are kept because they fail differently: one needs nothing
    configured and gets rate-limited, the other needs a key and does not.
    """
    cleaned = " ".join((query or "").split())
    if not cleaned:
        raise FetchError("a search needs something to search for")
    if len(cleaned) > _MAX_QUERY_CHARS:
        raise FetchError(
            f"that search is {len(cleaned)} characters; the limit is {_MAX_QUERY_CHARS}"
        )

    if choose_provider(provider, api_key) == "tavily":
        return _search_tavily(cleaned, limit=limit, transport=transport, api_key=api_key or "")
    return _search_duckduckgo(cleaned, limit=limit, transport=transport)


def _search_tavily(
    query: str,
    *,
    limit: int,
    transport: httpx.BaseTransport | None,
    api_key: str,
) -> SearchResults:
    """One POST to Tavily, and its JSON read into the same shape as the scrape's.

    The endpoint is fixed, so there is no address for the model to influence — but
    it goes through the same resolver check as a fetch, because "this tool only ever
    talks to one host" is exactly the assumption that stops being true quietly.
    """
    endpoint = assert_public_url(_TAVILY_URL)
    payload: dict[str, object] = {
        "query": query,
        "max_results": max(1, min(limit, _MAX_RESULTS)),
        "search_depth": _TAVILY_DEPTH,
        # Off: this tool returns results for the agent to read, and a generated
        # answer would be a second, unaudited voice in the transcript.
        "include_answer": False,
        "include_raw_content": False,
    }
    with httpx.Client(timeout=_TIMEOUT, transport=transport) as client:
        response = client.post(
            endpoint,
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "User-Agent": _USER_AGENT,
                "Accept": "application/json",
            },
        )

    if response.status_code in (401, 403):
        raise FetchError(
            "Tavily rejected the API key. Check it in Settings, or clear it to fall "
            "back to the keyless search."
        )
    if response.status_code == 429:
        raise FetchError("Tavily is rate-limiting this key; try again shortly.")
    if response.status_code in (432, 433):
        raise FetchError(
            "Tavily says this key is over its plan's usage limit. The key works; the "
            "plan does not have the credits."
        )
    if response.status_code >= 400:
        raise FetchError(f"Tavily answered HTTP {response.status_code}.")

    try:
        body = response.json()
    except ValueError as exc:
        raise FetchError("Tavily returned something that was not JSON.") from exc

    raw = body.get("results") if isinstance(body, dict) else None
    if not isinstance(raw, list):
        # A 200 without a results list is not "nothing matched" — it is a shape this
        # does not know, which is the distinction the caller reports.
        return SearchResults(query=query, provider="tavily", unreadable=True)

    # Cut to the caller's limit here rather than trusting `max_results` to have been
    # honoured: a provider that returns more than it was asked for must not turn
    # into a model that gets more than it asked for.
    hits: list[SearchHit] = []
    for item in raw[: max(1, limit)]:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        if not url.lower().startswith(("http://", "https://")):
            continue
        snippet = " ".join(str(item.get("content") or "").split())[:_MAX_SNIPPET_CHARS]
        title = " ".join(str(item.get("title") or "").split())
        hits.append(SearchHit(title=title or url, url=url, snippet=snippet))
    return SearchResults(query=query, hits=hits, provider="tavily")


def _search_duckduckgo(
    query: str,
    *,
    limit: int,
    transport: httpx.BaseTransport | None,
) -> SearchResults:
    """The keyless scrape: read the results out of DuckDuckGo's HTML.

    It can break without anything changing here, so a page it cannot read is an
    error naming that possibility, never an empty result list.
    """
    response = _get(
        _SEARCH_URL,
        transport=transport,
        accept="text/html,*/*;q=0.5",
        params={"q": query},
    )
    # The challenge comes back as 202 Accepted with a CAPTCHA, not as an error
    # status, so a status check alone would read it as a page of results.
    if response.status == 202 or _CHALLENGE_MARKER in response.body.lower():
        return SearchResults(query=query, provider="duckduckgo", blocked=True)

    parser = _ResultParser()
    parser.feed(response.body)
    parser.close()

    hits: list[SearchHit] = []
    for hit in parser.hits:
        # Anything that is not http(s) is not something the fetch tool could reach,
        # so offering it as a result would be offering a dead end.
        if not hit.url.lower().startswith(("http://", "https://")):
            continue
        hit.snippet = hit.snippet[:_MAX_SNIPPET_CHARS]
        hits.append(hit)

    return SearchResults(
        query=query,
        hits=hits[:limit],
        provider="duckduckgo",
        unreadable=not hits and not parser.saw_any_result,
    )
