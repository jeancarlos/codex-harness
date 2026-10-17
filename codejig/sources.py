import contextlib
import errno
import glob
import http.client
import ipaddress
import json
import os
import re
import socket
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from html.parser import HTMLParser

MAX_BYTES = 3_000_000
TIMEOUT = 20
USER_AGENT = "Mozilla/5.0 (compatible; code-jig check-sources)"
BLOCKED = {401, 403, 429, 451}
SKIP_TAGS = {"script", "style", "noscript", "template"}
TEXT_TYPES = ("text/", "application/xhtml", "application/xml", "application/json")
FAILING = {"dead", "quote-missing", "bad-url", "no-quote", "malformed"}
NAT64 = ipaddress.ip_network("64:ff9b::/96")
NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")


@dataclass
class Claim:
    where: str
    claim: str = ""
    url: str = ""
    quote: str = ""
    verdict: str = ""
    detail: str = ""


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, _attrs):
        self.skip += tag in SKIP_TAGS
        self.parts.append(" ")

    def handle_endtag(self, tag):
        self.skip -= tag in SKIP_TAGS and self.skip > 0
        self.parts.append(" ")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def normalize(text: str) -> str:
    return " ".join(re.sub(r"[\W_]+", " ", text.casefold()).split())


def page_text(body: bytes, ctype: str) -> str:
    charset = re.search(r"charset=([\w-]+)", ctype)
    try:
        text = body.decode(charset.group(1) if charset else "utf-8", errors="replace")
    except LookupError:
        text = body.decode("utf-8", errors="replace")
    if "html" in ctype or "xml" in ctype:
        parser = _Text()
        try:
            parser.feed(text)
        except AssertionError:
            pass
        text = "".join(parser.parts)
    return normalize(text)


def is_public_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip.split("%")[0])
    if addr in NAT64:
        return ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF).is_global
    return addr not in NAT64_LOCAL and addr.is_global


def is_public(host: str) -> bool | None:
    """True when every address the host resolves to is public, False when any is not,
    None when it does not resolve here."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return None
    return all(is_public_ip(info[4][0]) for info in infos)


def _target(url: str) -> tuple[str, str] | None:
    """(scheme, host) exactly as urllib and http.client will parse them for the connection."""
    try:
        req = urllib.request.Request(url)
        return req.type.lower(), http.client.HTTPConnection(req.host).host
    except (ValueError, http.client.InvalidURL):
        return None


def refused(url: str) -> str | None:
    """Why `url` must not be requested, or None. Through a proxy, a host that does not resolve here
    is refused: the proxy may resolve internal names this machine cannot."""
    target = _target(url)
    if target is None or target[0] not in ("http", "https") or not target[1]:
        return "only http and https are checked"
    public = is_public(target[1])
    if public is False:
        return f"{target[1]} is a private or local address"
    if public is None and proxied(url):
        return f"cannot confirm {target[1]} is public: it does not resolve here and a proxy would fetch it"
    return None


def proxied(url: str) -> bool:
    target = _target(url)
    proxies = urllib.request.getproxies()
    return bool(target) and target[0] in proxies and not urllib.request.proxy_bypass(target[1])


def _check_peer(conn: http.client.HTTPConnection) -> None:
    if conn._tunnel_host:
        return
    ip = conn.sock.getpeername()[0]
    if not is_public_ip(ip):
        conn.close()
        raise OSError(f"refused: {ip} is a private or local address")


class _HTTPConnection(http.client.HTTPConnection):
    def connect(self):
        super().connect()
        _check_peer(self)


class _HTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        super().connect()
        _check_peer(self)


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_HTTPConnection, req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_HTTPSConnection, req, context=self._context)


class _Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        reason = refused(newurl)
        if reason:
            raise urllib.error.URLError(f"redirect refused: {reason}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}), _HTTPHandler, _HTTPSHandler, _Redirects)
VIA_PROXY = urllib.request.build_opener(_Redirects)


def fetch(url: str) -> tuple[str, str]:
    """(verdict, page text or detail) for one URL, verdict "partial" when the page was cut at MAX_BYTES.
    Only public http and https addresses are requested, redirects included; without a proxy, the address
    the socket actually connected to is checked too."""
    reason = refused(url)
    if reason:
        return "bad-url", reason
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.9,*/*;q=0.5"})
    try:
        with (VIA_PROXY if proxied(url) else DIRECT).open(req, timeout=TIMEOUT) as r:
            ctype = r.headers.get("Content-Type", "")
            if not ctype.startswith(TEXT_TYPES):
                return "unreadable", ctype or "unknown type"
            body = r.read(MAX_BYTES + 1)
            return ("partial" if len(body) > MAX_BYTES else "ok"), page_text(body[:MAX_BYTES], ctype)
    except urllib.error.HTTPError as e:
        e.close()
        return ("blocked" if e.code in BLOCKED else "dead"), f"HTTP {e.code}"
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
        return "dead", str(getattr(e, "reason", e))


def expand(patterns: list[str]) -> list[str]:
    """File names with shell patterns expanded here, so PowerShell and cmd behave like a POSIX shell."""
    out = []
    for p in patterns:
        hits = (sorted(glob.glob(p)) or ([p] if os.path.exists(p) else [])) if glob.has_magic(p) else [p]
        if not hits:
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), p)
        out += hits
    return out


def _pre_verdict(c: Claim) -> None:
    if not c.url:
        c.verdict, c.detail = "malformed", "no url"
    elif not c.url.lower().startswith(("http://", "https://")):
        c.verdict, c.detail = "bad-url", "only http and https are checked"
    elif not normalize(c.quote):
        c.verdict, c.detail = "no-quote", "a claim needs an exact quote with words in it"


def _parse_line(where: str, line: str) -> Claim:
    c = Claim(where)
    try:
        row = json.loads(line)
        c.claim, c.url, c.quote = (str(row.get(k) or "").strip() for k in ("claim", "url", "quote"))
    except (ValueError, AttributeError):
        c.verdict, c.detail = "malformed", "not a JSON object"
        return c
    _pre_verdict(c)
    return c


def load(files: list[str]) -> list[Claim]:
    claims = []
    for name in expand(files):
        with contextlib.nullcontext(sys.stdin) if name == "-" else open(name, encoding="utf-8-sig") as f:
            claims += [_parse_line(f"{name}:{n}", line) for n, line in enumerate(f, 1) if line.strip()]
    return claims


def check(claims: list[Claim]) -> list[Claim]:
    todo = [c for c in claims if not c.verdict]
    urls = list(dict.fromkeys(c.url for c in todo))
    with ThreadPoolExecutor(max_workers=8) as pool:
        pages = dict(zip(urls, pool.map(fetch, urls), strict=True))
    for c in todo:
        verdict, text = pages[c.url]
        if verdict not in ("ok", "partial"):
            c.verdict, c.detail = verdict, text
        elif f" {normalize(c.quote)} " in f" {text} ":
            c.verdict = "ok"
        elif verdict == "partial":
            c.verdict, c.detail = "unreadable", f"the quote is not in the first {MAX_BYTES} bytes, the rest was not read"
        else:
            c.verdict, c.detail = "quote-missing", "the page does not contain the quote"
    return claims


def cli(args) -> int:
    if os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED") == "1":
        print("code-jig: network is disabled in this sandbox; run check-sources with escalation", file=sys.stderr)
        return 2
    try:
        claims = check(load(args.files))
    except OSError as e:
        print(f"code-jig: {e}", file=sys.stderr)
        return 2
    counts: dict[str, int] = {}
    for c in claims:
        counts[c.verdict] = counts.get(c.verdict, 0) + 1
        note = f" ({c.detail})" if c.detail else ""
        print(f"{c.verdict:13} {c.where}  {c.claim[:70]}  {c.url}{note}")
    print(f"{len(claims)} claims: " + ", ".join(f"{n} {v}" for v, n in sorted(counts.items(), key=lambda kv: kv[0] != "ok")))
    return 1 if FAILING & counts.keys() else 0
