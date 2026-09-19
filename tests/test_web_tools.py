"""Fetching a page, and the guards that make offering that reasonable.

The interesting tests here are the refusals. A fetch tool in a local agent is a way
for data to leave the machine and a way to reach the machine's own services; the
address checks, the manual redirects and the approval policy are the feature, and
the happy path is the easy part.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest

from surtitle.config import Settings
from surtitle.store.settings_store import PROVIDER_SPECS, SETTINGS_FIELDS
from surtitle.tools import web_tools
from surtitle.tools.fs_tools import ToolContext
from surtitle.tools.registry import (
    WEB_FETCH_TOOL,
    WEB_SEARCH_TOOL,
    ToolRegistry,
    _web_fetch_handler,
    _web_search_handler,
    default_tool_list,
)

PUBLIC = "93.184.216.34"
PRIVATE = "127.0.0.1"


@pytest.fixture
def resolves_public(monkeypatch):
    """Make DNS answer with a public address, so tests need no network."""

    def getaddrinfo(host, port, *args, **kwargs):
        return [(2, 1, 6, "", (PUBLIC, port or 443))]

    monkeypatch.setattr(web_tools.socket, "getaddrinfo", getaddrinfo)


def _page(body: bytes, *, status: int = 200, content_type: str = "text/html") -> httpx.Response:
    return httpx.Response(status, headers={"content-type": content_type}, content=body)


class TestWhatItRefuses:
    """The whole reason this is safe to turn on."""

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "data:text/plain,hello",
            "ftp://example.test/x",
            "not a url",
        ],
    )
    def test_only_http_and_https(self, url):
        with pytest.raises(web_tools.FetchError, match="http"):
            web_tools.assert_public_url(url)

    def test_a_url_carrying_credentials_is_refused(self, resolves_public):
        """The prompt shows the URL; a secret in it would be shown as ordinary."""
        with pytest.raises(web_tools.FetchError, match="username or password"):
            web_tools.assert_public_url("https://user:token@example.test/page")

    @pytest.mark.parametrize(
        "address",
        ["127.0.0.1", "10.0.0.5", "192.168.1.10", "172.16.4.4", "169.254.169.254", "0.0.0.0"],
    )
    def test_an_address_on_this_machine_or_its_network_is_refused(self, monkeypatch, address):
        """`169.254.169.254` is the cloud metadata endpoint, and `127.0.0.1:8765` is
        this application's own API."""

        def getaddrinfo(host, port, *args, **kwargs):
            return [(2, 1, 6, "", (address, port or 443))]

        monkeypatch.setattr(web_tools.socket, "getaddrinfo", getaddrinfo)
        with pytest.raises(web_tools.FetchError, match="not a public address"):
            web_tools.assert_public_url("https://example.test/page")

    def test_a_name_is_not_what_is_checked(self, monkeypatch):
        """A domain someone points at 10.0.0.1 is refused like the address is."""

        def getaddrinfo(host, port, *args, **kwargs):
            return [(2, 1, 6, "", ("10.0.0.1", port or 443))]

        monkeypatch.setattr(web_tools.socket, "getaddrinfo", getaddrinfo)
        with pytest.raises(web_tools.FetchError):
            web_tools.assert_public_url("https://innocent-looking.test/")

    def test_an_ipv4_address_wearing_an_ipv6_disguise_is_refused(self, monkeypatch):
        """`::ffff:127.0.0.1` is loopback, and `is_private` on the v6 form says no."""

        def getaddrinfo(host, port, *args, **kwargs):
            return [(10, 1, 6, "", ("::ffff:127.0.0.1", port or 443, 0, 0))]

        monkeypatch.setattr(web_tools.socket, "getaddrinfo", getaddrinfo)
        with pytest.raises(web_tools.FetchError, match="not a public address"):
            web_tools.assert_public_url("https://example.test/page")

    def test_a_name_that_does_not_resolve_says_so(self, monkeypatch):
        def getaddrinfo(host, port, *args, **kwargs):
            raise web_tools.socket.gaierror("nodename nor servname provided")

        monkeypatch.setattr(web_tools.socket, "getaddrinfo", getaddrinfo)
        with pytest.raises(web_tools.FetchError, match="could not resolve"):
            web_tools.assert_public_url("https://nowhere.test/")


class TestFetching:
    def test_html_becomes_readable_text(self, resolves_public):
        body = (
            b"<html><head><title>t</title><style>p{}</style></head><body>"
            b"<h1>Release notes</h1><p>Version 2 fixed the parser.</p>"
            b"<script>alert(1)</script><ul><li>one</li><li>two</li></ul></body></html>"
        )
        transport = httpx.MockTransport(lambda request: _page(body))

        page = web_tools.fetch_page("https://example.test/notes", transport=transport)

        assert "Release notes" in page.text
        assert "Version 2 fixed the parser." in page.text
        assert "one" in page.text and "two" in page.text
        assert "alert(1)" not in page.text, "a script is not text to read"
        assert "p{}" not in page.text, "and neither is a stylesheet"

    def test_plain_text_is_kept_as_it_is(self, resolves_public):
        transport = httpx.MockTransport(
            lambda request: _page(b"line one\nline two\n", content_type="text/plain")
        )

        page = web_tools.fetch_page("https://example.test/raw", transport=transport)

        assert page.text == "line one\nline two"

    def test_a_redirect_is_followed(self, resolves_public):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/old":
                return httpx.Response(301, headers={"location": "https://example.test/new"})
            return _page(b"<p>moved</p>")

        page = web_tools.fetch_page(
            "https://example.test/old", transport=httpx.MockTransport(handler)
        )

        assert page.url == "https://example.test/new"
        assert "moved" in page.text

    def test_a_redirect_to_the_local_network_is_refused(self, monkeypatch):
        """The attack the manual redirects exist for: a public URL that 302s home."""
        calls = {"n": 0}

        def getaddrinfo(host, port, *args, **kwargs):
            calls["n"] += 1
            address = PUBLIC if calls["n"] == 1 else PRIVATE
            return [(2, 1, 6, "", (address, port or 443))]

        monkeypatch.setattr(web_tools.socket, "getaddrinfo", getaddrinfo)
        transport = httpx.MockTransport(
            lambda request: httpx.Response(302, headers={"location": "http://localhost:8765/api"})
        )

        with pytest.raises(web_tools.FetchError, match="not a public address"):
            web_tools.fetch_page("https://example.test/thing", transport=transport)

    def test_a_redirect_loop_gives_up(self, resolves_public):
        transport = httpx.MockTransport(
            lambda request: httpx.Response(302, headers={"location": "https://example.test/again"})
        )

        with pytest.raises(web_tools.FetchError, match="redirected more than"):
            web_tools.fetch_page("https://example.test/loop", transport=transport)

    def test_a_binary_is_refused_rather_than_guessed_at(self, resolves_public):
        transport = httpx.MockTransport(
            lambda request: _page(b"%PDF-1.7", content_type="application/pdf")
        )

        with pytest.raises(web_tools.FetchError, match="not text"):
            web_tools.fetch_page("https://example.test/doc.pdf", transport=transport)

    def test_an_error_page_is_reported(self, resolves_public):
        transport = httpx.MockTransport(lambda request: _page(b"gone", status=404))

        with pytest.raises(web_tools.FetchError, match="answered 404"):
            web_tools.fetch_page("https://example.test/missing", transport=transport)

    def test_a_huge_page_is_cut_and_says_so(self, resolves_public):
        body = b"<p>" + b"x" * (web_tools._MAX_BYTES + 5_000) + b"</p>"
        transport = httpx.MockTransport(lambda request: _page(body))

        page = web_tools.fetch_page("https://example.test/huge", transport=transport)

        assert page.truncated is True


class TestThroughTheTool:
    """The tool turns every failure into a result the model can read.

    A raised exception would end the turn; the project's rule is that a tool failure
    is data.
    """

    def test_no_url_is_a_tool_error(self):
        result = _web_fetch_handler(ToolContext(root=__import__("pathlib").Path(".")))

        assert result.ok is False
        assert "URL" in (result.error or "")

    def test_a_refusal_becomes_a_result_not_an_exception(self, monkeypatch):
        def refuse(url):
            raise web_tools.FetchError("only http and https URLs can be fetched")

        monkeypatch.setattr(web_tools, "fetch_page", refuse)

        result = _web_fetch_handler(
            ToolContext(root=__import__("pathlib").Path(".")), url="file:///x"
        )

        assert result.ok is False
        assert "http" in (result.error or "")

    def test_an_unexpected_failure_is_also_a_result(self, monkeypatch):
        def explode(url):
            raise RuntimeError("socket exploded")

        monkeypatch.setattr(web_tools, "fetch_page", explode)

        result = _web_fetch_handler(
            ToolContext(root=__import__("pathlib").Path(".")), url="https://x.test/"
        )

        assert result.ok is False
        assert "socket exploded" in (result.error or "")

    def test_a_long_page_is_clipped_and_the_model_is_told(self, monkeypatch):
        from surtitle.tools.registry import _WEB_FETCH_CHARS

        monkeypatch.setattr(
            web_tools,
            "fetch_page",
            lambda url: web_tools.FetchedPage(
                url=str(url),
                status=200,
                content_type="text/html",
                text="y" * (_WEB_FETCH_CHARS + 100),
            ),
        )

        result = _web_fetch_handler(
            ToolContext(root=__import__("pathlib").Path(".")), url="https://x.test/"
        )

        assert result.ok is True
        assert "beginning of" in (result.data or {}).get("text", "")

    def test_the_url_is_in_what_the_user_is_asked_to_approve(self):
        """The URL is the part that can carry something out, so it is the part shown."""
        tool = next(t for t in default_tool_list() if t.name == WEB_FETCH_TOOL)

        assert tool.approval == "ask", "reading is harmless; requesting is not"
        assert tool.mutating is False

    def test_a_sub_agent_cannot_be_given_it(self):
        """A child has a fresh approval broker and nobody watching it: an `ask` tool
        inside one would wait forever."""
        child = ToolRegistry(default_tool_list()).read_only()

        assert WEB_FETCH_TOOL not in child.names()


def _results_html(*rows: tuple[str, str, str]) -> bytes:
    """A result list shaped like the endpoint's, for the parser to read.

    Written from a real response, because the parser keys on the endpoint's class
    names — which is the fragile part of a scrape, and the reason a page that does
    not look like this has to be reported as unreadable rather than as empty.
    """
    parts = []
    for title, url, snippet in rows:
        wrapped = f"//duckduckgo.com/l/?uddg={quote(url, safe='')}&rut=abc123"
        parts.append(
            f'<div class="result"><h2 class="result__title">'
            f'<a rel="nofollow" class="result__a" href="{wrapped}">{title}</a></h2>'
            f'<a class="result__snippet" href="{wrapped}">{snippet}</a></div>'
        )
    return ("<html><body>" + "".join(parts) + "</body></html>").encode()


class TestSearching:
    def test_results_come_back_with_their_real_addresses(self, resolves_public):
        """The href in the markup is DuckDuckGo's own redirect. The model must be
        given where the result actually goes."""
        transport = httpx.MockTransport(
            lambda request: _page(
                _results_html(
                    ("Release notes", "https://example.test/notes", "Version 2 fixed it."),
                    ("Changelog", "https://example.test/log", "What changed, and when."),
                )
            )
        )

        found = web_tools.search("release notes", transport=transport)

        assert [hit.url for hit in found.hits] == [
            "https://example.test/notes",
            "https://example.test/log",
        ]
        assert found.hits[0].title == "Release notes"
        assert found.hits[0].snippet == "Version 2 fixed it."
        assert found.unreadable is False

    def test_the_query_is_sent_as_the_endpoint_expects(self, resolves_public):
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return _page(_results_html(("A", "https://example.test/a", "s")))

        web_tools.search("  two   words  ", transport=httpx.MockTransport(handler))

        assert "html.duckduckgo.com" in seen["url"]
        assert "q=two+words" in seen["url"], "whitespace collapsed, not sent as typed"

    def test_the_bot_challenge_is_reported_as_blocked(self, resolves_public):
        """Measured against the real endpoint: the first query returned results and
        every query after it got this page — as `202 Accepted`, not as an error
        status. It is the commonest outcome of a keyless scrape, so it has to be
        distinguishable from both 'nothing matched' and 'the markup moved'."""
        body = (
            b"<html><body>Unfortunately, bots use DuckDuckGo too. Please complete the "
            b"following challenge to confirm this search was made by a human.</body></html>"
        )
        transport = httpx.MockTransport(lambda request: _page(body, status=202))

        found = web_tools.search("anything", transport=transport)

        assert found.blocked is True
        assert found.hits == []
        assert found.unreadable is False, "blocked and unreadable are different answers"

    def test_the_challenge_is_recognised_even_if_the_status_changes(self, resolves_public):
        """The marker is checked as well as the 202, so a change on their side does
        not turn 'you are rate-limited' into 'the markup moved'."""
        body = b"<html><body>Please complete the following challenge.</body></html>"
        transport = httpx.MockTransport(lambda request: _page(body, status=200))

        found = web_tools.search("anything", transport=transport)

        assert found.blocked is True

    def test_a_page_this_cannot_read_is_not_an_empty_search(self, resolves_public):
        """The distinction the whole tool turns on: 'the markup moved' and 'nothing
        matched' are different things to tell somebody."""
        transport = httpx.MockTransport(
            lambda request: _page(b"<html><body><p>A new layout.</p></body></html>")
        )

        found = web_tools.search("anything", transport=transport)

        assert found.hits == []
        assert found.unreadable is True

    def test_a_page_of_results_with_nothing_usable_is_not_unreadable(self, resolves_public):
        """The endpoint did not once produce an empty result page in testing — even a
        nonsense query came back with fuzzy matches — so 'readable but empty' is the
        state for a page whose results this tool cannot use, not for a real 'nothing
        matched'. Saying which is which is the point."""
        body = b'<html><body><a class="result__a" href="javascript:alert(1)">nope</a></body></html>'
        transport = httpx.MockTransport(lambda request: _page(body))

        found = web_tools.search("anything", transport=transport)

        assert found.hits == []
        assert found.unreadable is False
        assert found.blocked is False

    def test_only_the_requested_number_comes_back(self, resolves_public):
        rows = tuple((f"R{n}", f"https://example.test/{n}", "s") for n in range(10))
        transport = httpx.MockTransport(lambda request: _page(_results_html(*rows)))

        found = web_tools.search("many", limit=3, transport=transport)

        assert len(found.hits) == 3

    def test_a_result_that_is_not_a_web_address_is_dropped(self, resolves_public):
        """Offering the model something `web_fetch` would refuse is a dead end."""
        body = (
            b'<html><body><a class="result__a" href="javascript:alert(1)">nope</a>'
            b'<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fok.test%2F">yes</a>'
            b"</body></html>"
        )
        transport = httpx.MockTransport(lambda request: _page(body))

        found = web_tools.search("mixed", transport=transport)

        assert [hit.url for hit in found.hits] == ["https://ok.test/"]

    def test_an_empty_query_is_refused(self, resolves_public):
        with pytest.raises(web_tools.FetchError, match="search for"):
            web_tools.search("   ", transport=httpx.MockTransport(lambda r: _page(b"")))

    def test_an_overlong_query_is_refused_rather_than_sent(self, resolves_public):
        with pytest.raises(web_tools.FetchError, match="limit"):
            web_tools.search("x" * 5000, transport=httpx.MockTransport(lambda r: _page(b"")))


class TestSearchingThroughTheTool:
    def test_the_results_are_given_to_the_model(self, monkeypatch):
        monkeypatch.setattr(
            web_tools,
            "search",
            lambda query, **_kwargs: web_tools.SearchResults(
                query=query,
                hits=[web_tools.SearchHit("T", "https://example.test/", "S")],
            ),
        )

        result = _web_search_handler(ToolContext(root=Path(".")), query="anything")

        assert result.ok is True
        assert (result.data or {})["results"][0]["url"] == "https://example.test/"
        assert "snippet is not the page" in (result.data or {})["note"]

    def test_an_unreadable_page_says_so_rather_than_nothing_found(self, monkeypatch):
        monkeypatch.setattr(
            web_tools,
            "search",
            lambda query, **_kwargs: web_tools.SearchResults(query=query, unreadable=True),
        )

        result = _web_search_handler(ToolContext(root=Path(".")), query="anything")

        assert result.ok is False
        assert "could not read" in (result.error or "")
        assert "nothing exists" in (result.error or ""), "and warns against that conclusion"

    def test_a_broken_endpoint_is_a_result_not_an_exception(self, monkeypatch):
        def explodes(query, **_kwargs):
            raise RuntimeError("connection reset")

        monkeypatch.setattr(web_tools, "search", explodes)

        result = _web_search_handler(ToolContext(root=Path(".")), query="anything")

        assert result.ok is False
        assert "Could not search" in (result.error or "")

    def test_no_query_is_a_tool_error(self):
        result = _web_search_handler(ToolContext(root=Path(".")), query="  ")

        assert result.ok is False

    def test_the_query_is_what_the_user_is_asked_to_approve(self):
        """The query is the part of a search that leaves the machine."""
        tool = next(t for t in default_tool_list() if t.name == WEB_SEARCH_TOOL)

        assert tool.approval == "ask"
        assert tool.mutating is False

    def test_a_sub_agent_cannot_be_given_it(self):
        child = ToolRegistry(default_tool_list()).read_only()

        assert WEB_SEARCH_TOOL not in child.names()


def _tavily_body(*rows: tuple[str, str, str]) -> bytes:
    import json as _json

    return _json.dumps(
        {
            "query": "q",
            "results": [
                {"title": title, "url": url, "content": content, "score": 0.9}
                for title, url, content in rows
            ],
        }
    ).encode()


class TestSearchingWithATavilyKey:
    """The keyed route: a real search API instead of a scrape of one.

    Optional on purpose — a machine with no key still searches — so the tests that
    matter are that the key is *used* when it is there, that a key is never echoed
    back, and that the ways a key can fail are reported as what they are rather than
    as an empty web.
    """

    def test_the_key_is_sent_and_the_question_is_asked(self, resolves_public):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            seen["body"] = json.loads(request.content)
            return _page(_tavily_body(("T", "https://example.test/a", "s")))

        found = web_tools.search(
            "who won", api_key="tvly-secret", transport=httpx.MockTransport(handler)
        )

        assert seen["url"].startswith("https://api.tavily.com/search")
        assert seen["auth"] == "Bearer tvly-secret"
        assert seen["body"]["query"] == "who won"
        assert seen["body"]["search_depth"] == "basic"
        assert seen["body"]["include_answer"] is False, "no second voice in the transcript"
        assert found.provider == "tavily"

    def test_results_become_hits(self, resolves_public):
        transport = httpx.MockTransport(
            lambda request: _page(
                _tavily_body(
                    ("Release notes", "https://example.test/notes", "Version 2 fixed it."),
                    ("Changelog", "https://example.test/log", "What changed, and when."),
                )
            )
        )

        found = web_tools.search("release notes", api_key="tvly-k", transport=transport)

        assert [hit.url for hit in found.hits] == [
            "https://example.test/notes",
            "https://example.test/log",
        ]
        assert found.hits[0].title == "Release notes"
        assert found.hits[0].snippet == "Version 2 fixed it."
        assert found.unreadable is False and found.blocked is False

    def test_the_number_asked_for_is_the_number_that_comes_back(self, resolves_public):
        rows = tuple((f"R{n}", f"https://example.test/{n}", "s") for n in range(10))
        transport = httpx.MockTransport(lambda request: _page(_tavily_body(*rows)))

        found = web_tools.search("many", limit=3, api_key="tvly-k", transport=transport)

        assert len(found.hits) == 3

    def test_a_bad_key_says_so_rather_than_nothing_was_found(self, resolves_public):
        transport = httpx.MockTransport(lambda request: _page(b"{}", status=401))

        with pytest.raises(web_tools.FetchError, match="rejected the API key"):
            web_tools.search("anything", api_key="tvly-bad", transport=transport)

    def test_a_rate_limit_and_a_plan_limit_are_different_answers(self, resolves_public):
        limited = httpx.MockTransport(lambda request: _page(b"{}", status=429))
        with pytest.raises(web_tools.FetchError, match="rate-limiting"):
            web_tools.search("x", api_key="tvly-k", transport=limited)

        over = httpx.MockTransport(lambda request: _page(b"{}", status=432))
        with pytest.raises(web_tools.FetchError, match="usage limit"):
            web_tools.search("x", api_key="tvly-k", transport=over)

    def test_a_server_error_is_reported_with_its_status(self, resolves_public):
        transport = httpx.MockTransport(lambda request: _page(b"", status=503))

        with pytest.raises(web_tools.FetchError, match="503"):
            web_tools.search("x", api_key="tvly-k", transport=transport)

    def test_a_200_without_results_is_not_an_empty_web(self, resolves_public):
        transport = httpx.MockTransport(lambda request: _page(b'{"unexpected": true}'))

        found = web_tools.search("x", api_key="tvly-k", transport=transport)

        assert found.hits == []
        assert found.unreadable is True

    def test_a_result_that_is_not_a_web_address_is_dropped(self, resolves_public):
        body = json.dumps(
            {
                "results": [
                    {"title": "no", "url": "javascript:alert(1)"},
                    {"title": "yes", "url": "https://ok.test/"},
                ]
            }
        ).encode()
        transport = httpx.MockTransport(lambda request: _page(body))

        found = web_tools.search("x", api_key="tvly-k", transport=transport)

        assert [hit.url for hit in found.hits] == ["https://ok.test/"]

    def test_without_a_key_the_scrape_is_still_what_runs(self, resolves_public):
        """A machine with no key has to keep working: that is the whole reason the
        keyless path exists."""
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["host"] = request.url.host
            return _page(_results_html(("A", "https://example.test/a", "s")))

        found = web_tools.search("anything", transport=httpx.MockTransport(handler))

        assert seen["host"] == "html.duckduckgo.com"
        assert found.provider == "duckduckgo"


class TestTheKeyedProviderThroughTheTool:
    def test_the_key_from_settings_is_what_is_used(self, monkeypatch):
        seen: dict = {}

        def fake(query, **kwargs):
            seen["api_key"] = kwargs.get("api_key")
            return web_tools.SearchResults(query=query, hits=[], provider="tavily")

        monkeypatch.setattr(web_tools, "search", fake)
        settings = Settings(DEEPSEEK_API_KEY="k", TAVILY_API_KEY="tvly-from-settings")

        result = _web_search_handler(
            ToolContext(root=Path("."), settings=settings), query="anything"
        )

        assert seen["api_key"] == "tvly-from-settings"
        assert result.ok is True
        assert (result.data or {})["provider"] == "tavily"

    def test_a_settings_object_without_one_falls_back_to_the_scrape(self, monkeypatch):
        seen: dict = {}

        def fake(query, **kwargs):
            seen["api_key"] = kwargs.get("api_key")
            return web_tools.SearchResults(query=query, hits=[])

        monkeypatch.setattr(web_tools, "search", fake)

        _web_search_handler(
            ToolContext(root=Path("."), settings=Settings(DEEPSEEK_API_KEY="k")), query="q"
        )

        assert seen["api_key"] is None

    def test_the_key_never_reaches_the_model_or_the_screen(self, monkeypatch):
        """The rule for every credential in this application: a secret does not
        travel toward the UI — not in the result, not in the display line, not in an
        error."""
        secret = "tvly-should-not-appear"

        def fake(query, **_kwargs):
            return web_tools.SearchResults(
                query=query,
                hits=[web_tools.SearchHit("T", "https://x.test/", "S")],
                provider="tavily",
            )

        monkeypatch.setattr(web_tools, "search", fake)
        settings = Settings(DEEPSEEK_API_KEY="k", TAVILY_API_KEY=secret)

        result = _web_search_handler(
            ToolContext(root=Path("."), settings=settings), query="anything"
        )

        assert secret not in json.dumps(result.data)
        assert secret not in (result.display or "")
        assert secret not in (result.error or "")

    def test_the_settings_panel_knows_about_the_provider(self):
        """The panel is built from these specs, so the key can be pasted in, tested
        and cleared without anything else being written for it."""
        spec = PROVIDER_SPECS["tavily"]

        assert spec.api_key_env == "TAVILY_API_KEY"
        assert spec.discovery_path == "/usage", "a real authenticated call, and free"
        # Optional: nothing requires it, and the doctor must not ask for it.
        assert Settings(DEEPSEEK_API_KEY="k").needs_credential("TAVILY_API_KEY") is False


class TestChoosingTheSearchProvider:
    """Search can be told which provider to use, and the choice is the user's.

    Both are kept because they fail differently: the keyless one needs nothing
    configured and gets rate-limited, the keyed one needs a key and does not.
    """

    def test_automatic_takes_the_key_when_there_is_one(self):
        assert web_tools.choose_provider("automatic", "tvly-k") == "tavily"
        assert web_tools.choose_provider("automatic", None) == "duckduckgo"
        assert web_tools.choose_provider(None, "tvly-k") == "tavily", "unset means automatic"

    def test_duckduckgo_can_be_asked_for_even_with_a_key(self):
        """Somebody with a key may still prefer the scrape — to keep queries off a
        third party, or to compare the two."""
        assert web_tools.choose_provider("duckduckgo", "tvly-k") == "duckduckgo"

    def test_tavily_without_a_key_is_a_mistake_that_is_reported(self):
        """Not a silent fallback: the user asked for the provider they pay for, and
        getting a scrape instead would be invisible in the results."""
        with pytest.raises(web_tools.FetchError, match="needs an API key"):
            web_tools.choose_provider("tavily", None)

    def test_the_case_and_spacing_of_the_setting_do_not_matter(self):
        assert web_tools.choose_provider("  TAVILY ", "tvly-k") == "tavily"
        assert web_tools.choose_provider("DuckDuckGo", "tvly-k") == "duckduckgo"

    def test_an_unknown_value_does_not_break_search(self):
        """A stored setting from a newer version, or a typo by hand: the useful
        default is better than a refusal."""
        assert web_tools.choose_provider("bing", "tvly-k") == "tavily"
        assert web_tools.choose_provider("bing", None) == "duckduckgo"

    def test_the_chosen_provider_is_the_one_asked(self, resolves_public):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["host"] = request.url.host
            if request.url.host == "api.tavily.com":
                return _page(_tavily_body(("T", "https://example.test/a", "s")))
            return _page(_results_html(("A", "https://example.test/a", "s")))

        transport = httpx.MockTransport(handler)

        forced_scrape = web_tools.search(
            "x", api_key="tvly-k", provider="duckduckgo", transport=transport
        )
        assert seen["host"] == "html.duckduckgo.com"
        assert forced_scrape.provider == "duckduckgo"

        forced_api = web_tools.search("x", api_key="tvly-k", provider="tavily", transport=transport)
        assert seen["host"] == "api.tavily.com"
        assert forced_api.provider == "tavily"

    def test_the_tool_passes_the_stored_preference_through(self, monkeypatch):
        seen: dict = {}

        def fake(query, **kwargs):
            seen.update(kwargs)
            return web_tools.SearchResults(query=query, hits=[])

        monkeypatch.setattr(web_tools, "search", fake)
        settings = Settings(
            DEEPSEEK_API_KEY="k", SURTITLE_SEARCH_PROVIDER="duckduckgo", TAVILY_API_KEY="tvly-k"
        )

        _web_search_handler(ToolContext(root=Path("."), settings=settings), query="q")

        assert seen["provider"] == "duckduckgo"
        assert seen["api_key"] == "tvly-k", "the key is still there to be used if asked"

    def test_the_tool_reports_a_provider_that_cannot_be_used(self, monkeypatch):
        """The mistake is the user's to fix, so it is said rather than swallowed."""
        settings = Settings(DEEPSEEK_API_KEY="k", SURTITLE_SEARCH_PROVIDER="tavily")

        result = _web_search_handler(ToolContext(root=Path("."), settings=settings), query="q")

        assert result.ok is False
        assert "API key" in (result.error or "")

    def test_the_setting_exists_for_the_settings_screen_to_offer(self):
        field = next(f for f in SETTINGS_FIELDS if f.name == "search_provider")

        assert field.section == "search"
        assert field.choices == web_tools.SEARCH_PROVIDERS
