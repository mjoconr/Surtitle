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
  request* is not, and the same request through `run_shell` is already gated.
* **It cannot be made to return a gigabyte.** The body is read up to a limit, only
  text-ish content types are accepted, and what comes back says how much was left
  out.

Not covered, and worth knowing: the address is validated and then the connection is
made by hostname, so a name server that answers differently between the two lookups
could still land on a private address. Pinning the validated address would close
that; it also breaks virtual hosting and TLS verification, which is why it is not
done here. This is a single-user tool on the user's own machine, not a service
fetching on behalf of strangers.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import ClassVar
from urllib.parse import urlsplit

import httpx

__all__ = ["FetchError", "FetchedPage", "assert_public_url", "fetch_page", "html_to_text"]

# Redirects are followed one at a time so each hop can be validated. Five is more
# than any documentation site needs and few enough that a loop cannot run long.
_MAX_REDIRECTS = 5
# The connection and the read, separately: a slow site should not hold a turn open
# forever, and the read is bounded again by _MAX_BYTES.
_TIMEOUT = httpx.Timeout(10.0, read=15.0)
_MAX_BYTES = 2 * 1024 * 1024
_USER_AGENT = "Surtitle/1.0 (+https://github.com/mjoconr/Surtitle)"

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
    current = assert_public_url(url)
    with httpx.Client(
        timeout=_TIMEOUT,
        follow_redirects=False,
        headers={"User-Agent": _USER_AGENT, "Accept": "text/html,text/plain,*/*;q=0.5"},
        transport=transport,
    ) as client:
        for _ in range(_MAX_REDIRECTS + 1):
            response = client.get(current)
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
            text = body.decode(response.encoding or "utf-8", errors="replace")
            if "html" in content_type or text.lstrip()[:1] == "<":
                text = html_to_text(text)
            return FetchedPage(
                url=str(response.url),
                status=response.status_code,
                content_type=content_type or "unknown",
                text=text.strip(),
                truncated=truncated,
            )
    raise FetchError(f"{url} redirected more than {_MAX_REDIRECTS} times")
