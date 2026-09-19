"""Fetching a page, and the guards that make offering that reasonable.

The interesting tests here are the refusals. A fetch tool in a local agent is a way
for data to leave the machine and a way to reach the machine's own services; the
address checks, the manual redirects and the approval policy are the feature, and
the happy path is the easy part.
"""

from __future__ import annotations

import httpx
import pytest

from surtitle.tools import web_tools
from surtitle.tools.fs_tools import ToolContext
from surtitle.tools.registry import (
    WEB_FETCH_TOOL,
    ToolRegistry,
    _web_fetch_handler,
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
