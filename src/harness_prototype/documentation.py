"""Bounded HTTPS retrieval and readable-text extraction for documentation pages."""

from datetime import datetime, timezone
from html.parser import HTMLParser
from ipaddress import ip_address
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .tool_results import tool_error, tool_success

DOCUMENTATION_TIMEOUT_SECONDS = 10
MAX_DOCUMENTATION_BYTES = 256 * 1024
MAX_DOCUMENTATION_CHARACTERS = 12_000
SUPPORTED_CONTENT_TYPES = {"text/html", "text/plain"}


def _validate_documentation_url(url: str) -> str | None:
    """Return a diagnostic when a URL is not an allowed public HTTPS target."""
    if (
        not isinstance(url, str) or not url
        or any(not char.isprintable() or char.isspace() for char in url)
    ):
        return "URL must be a nonempty printable string."
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return "URL is invalid."
    if parsed.scheme.casefold() != "https":
        return "Only HTTPS documentation URLs are allowed."
    if (
        not parsed.hostname or parsed.username is not None or parsed.password is not None
        or port is not None and not 1 <= port <= 65535
    ):
        return "URL must contain a valid host and must not contain credentials."
    hostname = parsed.hostname.rstrip(".").casefold()
    if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
        return "Local documentation hosts are blocked."
    try:
        address = ip_address(hostname)
    except ValueError:
        return None
    if not address.is_global:
        return "Private, loopback, link-local, and reserved IP hosts are blocked."
    return None


class _SafeRedirectHandler(HTTPRedirectHandler):
    """Reject redirects that leave the permitted HTTPS/public-host policy."""

    def redirect_request(
        self,
        request: Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> Request | None:
        """Validate the redirect destination before another request is sent."""
        error = _validate_documentation_url(new_url)
        if error:
            raise URLError(f"Redirect blocked: {error}")
        return super().redirect_request(
            request, file_pointer, code, message, headers, new_url,
        )


class _ReadableHTMLParser(HTMLParser):
    """Collect visible document text while excluding common non-content elements."""

    _IGNORED = {
        "aside", "canvas", "footer", "header", "nav", "noscript", "script",
        "style", "svg", "template",
    }
    _BREAKS = {
        "address", "article", "aside", "blockquote", "br", "dd", "div", "dl",
        "dt", "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5",
        "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section",
        "table", "td", "th", "tr", "ul",
    }

    def __init__(self) -> None:
        """Initialize text collection and ignored-element nesting state."""
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Track ignored elements and insert boundaries around block content."""
        if tag in self._IGNORED:
            self.ignored_depth += 1
        elif not self.ignored_depth and tag in self._BREAKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        """Close ignored elements and preserve block boundaries."""
        if tag in self._IGNORED and self.ignored_depth:
            self.ignored_depth -= 1
        elif not self.ignored_depth and tag in self._BREAKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        """Collect visible character data only."""
        if not self.ignored_depth:
            self.parts.append(data)

    def readable_text(self) -> str:
        """Normalize whitespace while retaining useful paragraph boundaries."""
        lines = [" ".join(line.split()) for line in "".join(self.parts).splitlines()]
        return "\n".join(line for line in lines if line)


def fetch_documentation(url: str) -> dict:
    """Fetch bounded readable text from a public HTTPS documentation page.

    Args:
        url: Absolute public HTTPS page URL. Local and non-global IP hosts are blocked.
    """
    validation_error = _validate_documentation_url(url)
    if validation_error:
        return tool_error(validation_error, status="invalid_url", source_url=url)

    try:
        request = Request(url, headers={
            "Accept": "text/html, text/plain;q=0.9",
            "User-Agent": "FLUX/0.1 documentation-fetcher",
        })
        with build_opener(_SafeRedirectHandler()).open(
            request, timeout=DOCUMENTATION_TIMEOUT_SECONDS,
        ) as response:
            final_url = response.geturl()
            redirect_error = _validate_documentation_url(final_url)
            if redirect_error:
                return tool_error(
                    f"Final URL blocked: {redirect_error}",
                    status="blocked_redirect", source_url=url,
                )
            content_type = response.headers.get_content_type().casefold()
            if content_type not in SUPPORTED_CONTENT_TYPES:
                return tool_error(
                    f"Unsupported documentation content type: {content_type}",
                    status="unsupported_content_type", source_url=final_url,
                )
            charset = response.headers.get_content_charset() or "utf-8"
            body = response.read(MAX_DOCUMENTATION_BYTES + 1)
    except HTTPError as error:
        return tool_error(
            f"Documentation request failed with HTTP status {error.code}.",
            status="http_error", source_url=url,
        )
    except ValueError as error:
        return tool_error(
            f"Documentation URL is invalid: {error}",
            status="invalid_url", source_url=url,
        )
    except (URLError, TimeoutError, OSError) as error:
        reason = getattr(error, "reason", error)
        return tool_error(
            f"Documentation request failed: {reason}",
            status="network_error", source_url=url,
        )

    byte_truncated = len(body) > MAX_DOCUMENTATION_BYTES
    body = body[:MAX_DOCUMENTATION_BYTES]
    try:
        decoded = body.decode(charset)
    except (LookupError, UnicodeDecodeError):
        return tool_error(
            f"Documentation response could not be decoded as {charset} text.",
            status="parsing_error", source_url=final_url,
        )

    try:
        if content_type == "text/html":
            parser = _ReadableHTMLParser()
            parser.feed(decoded)
            parser.close()
            text = parser.readable_text()
        else:
            text = decoded
    except (ValueError, TypeError) as error:
        return tool_error(
            f"Documentation response could not be parsed: {error}",
            status="parsing_error", source_url=final_url,
        )
    if not text.strip():
        return tool_error(
            "Documentation response contained no readable text.",
            status="parsing_error", source_url=final_url,
        )

    character_truncated = len(text) > MAX_DOCUMENTATION_CHARACTERS
    truncated = byte_truncated or character_truncated
    text = text[:MAX_DOCUMENTATION_CHARACTERS]
    return tool_success(
        complete=not truncated,
        truncated=truncated,
        status="retrieved",
        source_url=final_url,
        retrieved_at=datetime.now(timezone.utc).isoformat(),
        content_type=content_type,
        text=text,
        bytes_read=len(body),
        byte_limit=MAX_DOCUMENTATION_BYTES,
        character_limit=MAX_DOCUMENTATION_CHARACTERS,
    )
