
"""Collect bounded public evidence from the supplied application and company URLs."""

import http.client
import ipaddress
import re
import socket
import time
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit


@dataclass
class CompanyContext:
    sources: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.links = []
        self.href = None
        self.label = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.hidden += 1
        if tag == "a":
            self.href = dict(attrs).get("href")
            self.label = []

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self.hidden:
            self.hidden -= 1
        if tag == "a" and self.href:
            self.links.append((self.href, " ".join(self.label).lower()))
            self.href = None

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.parts.append(data.strip())
            if self.href:
                self.label.append(data.strip())


def _fetch(url, deadline):
    for _ in range(4):
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Unsupported source URL")
        default_port = 443 if parsed.scheme == "https" else 80
        if parsed.port not in (None, default_port):
            raise ValueError("Unsupported source port")
        ips = list(dict.fromkeys(
            item[4][0] for item in socket.getaddrinfo(parsed.hostname, default_port, type=socket.SOCK_STREAM)
        ))
        if not ips or any(not ipaddress.ip_address(ip).is_global or ipaddress.ip_address(ip).is_multicast for ip in ips):
            raise ValueError("Source must resolve to public unicast addresses")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Company evidence lookup timed out")
        connection_class = http.client.HTTPSConnection if default_port == 443 else http.client.HTTPConnection
        # Pin the validated address while retaining the original hostname for TLS.
        with_connection = connection_class(parsed.hostname, default_port, timeout=min(4, remaining))
        with_connection._create_connection = lambda address, timeout, source: socket.create_connection(
            (ips[0], address[1]), timeout, source
        )
        try:
            target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
            with_connection.request("GET", target, headers={"Accept": "text/html,text/plain", "User-Agent": "JobHuntAIStudio/1.0"})
            response = with_connection.getresponse()
            if response.status in (301, 302, 303, 307, 308) and response.getheader("Location"):
                url = urljoin(url, response.getheader("Location"))
                continue
            content_type = response.headers.get_content_type()
            if response.status != 200 or content_type not in ("text/html", "application/xhtml+xml", "text/plain"):
                raise ValueError("Source unavailable or not a supported page")
            chunks = []
            size = 0
            while time.monotonic() < deadline:
                chunk = response.read1(16 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > 512 * 1024:
                    raise ValueError("Source page exceeds the size limit")
                chunks.append(chunk)
            else:
                raise TimeoutError("Company evidence lookup timed out")
            page = Page()
            page.feed(b"".join(chunks).decode(response.headers.get_content_charset() or "utf-8", errors="replace"))
            return url, page
        finally:
            with_connection.close()
    raise ValueError("Too many source redirects")


def collect_company_context(job):
    context = CompanyContext()
    deadline = time.monotonic() + 12
    queue = [(job.get("url"), "application"), (job.get("company_website"), "official")]
    attempted = set()
    for url, kind in queue:
        if not url or url == "N/A" or url in attempted or len(attempted) >= 3:
            continue
        attempted.add(url)
        try:
            final, page = _fetch(url, deadline)
        except (OSError, ValueError, LookupError, http.client.HTTPException):
            context.warnings.append(f"The {kind} source was unavailable. Review can use the supplied job description.")
            continue
        excerpt = re.sub(r"\s+", " ", " ".join(page.parts)).strip()[:6000]
        if not excerpt:
            context.warnings.append(f"The {kind} source contained no readable text.")
            continue
        context.sources.append({"url": final, "kind": kind, "excerpt": excerpt})
        for href, label in page.links:
            try:
                linked = urljoin(final, href)
                target = urlsplit(linked)
                same_site = target.hostname == urlsplit(final).hostname
            except ValueError:
                continue
            if target.scheme not in ("http", "https") or not target.hostname:
                continue
            if linked != final and (
                label in ("company website", "official website", "visit website")
                or (kind == "official" and same_site and label in ("about", "about us", "careers"))
            ):
                queue.append((linked, "official"))
                if len(queue) >= 5:
                    break
    if not any(s["kind"] == "official" for s in context.sources):
        context.warnings.append("No official company page was confirmed. Review uses available posting and JD evidence.")
    return context.to_dict()
