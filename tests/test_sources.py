import contextlib
import http.server
import io
import json
import os
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from codejig import sources
from tests.util import tree, write

REAL_IS_PUBLIC_IP = sources.is_public_ip

PAGES = {
    "/doc": (200, "text/html; charset=utf-8",
             ("<html><head><script>var hidden = 'not text';</script><style>p{}</style></head>"
              "<body><p>The default limit is <b>32&nbsp;KiB</b> and Codex \u201cstops adding\u201d files.</p></body></html>")),
    "/plain": (200, "text/plain", "line one\nline   two"),
    "/pep": (200, "text/html", "<dl><dt>Python-Version</dt><dd>: 3.11</dd></dl><p>The <code>tomllib</code> module parses TOML.</p>"),
    "/blocked": (403, "text/html", "no"),
    "/gone": (404, "text/html", "missing"),
    "/pdf": (200, "application/pdf", "%PDF-1.7"),
    "/moved": (301, "text/html", ""),
    "/to-private": (301, "text/html", ""),
    "/mb4": (200, "text/html; charset=utf8mb4", "<p>caf\u00e9 opens at nine</p>"),
    "/short": (200, "text/plain", "ff\r\ncut short"),
}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        status, ctype, body = PAGES.get(self.path, (404, "text/plain", ""))
        self.send_response(status)
        if status == 301:
            self.send_header("Location", "http://10.0.0.1/secret" if self.path == "/to-private" else "/doc")
        self.send_header("Content-Type", ctype)
        if self.path == "/short":
            self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *args):
        pass


class Sources(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        real = sources.is_public_ip
        patcher = mock.patch.object(sources, "is_public_ip", lambda ip: ip == "127.0.0.1" or real(ip))
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_private_and_local_addresses_are_refused(self):
        for url in ("http://10.0.0.1/x", "http://169.254.169.254/latest/meta-data/", "http://127.0.0.2:1/x", "http://[::1]/x"):
            with self.subTest(url=url):
                self.assertEqual(sources.fetch(url)[0], "bad-url")

    def test_the_connected_address_is_checked_not_only_the_dns_answer(self):
        port = self.server.server_address[1]
        with mock.patch.object(sources, "is_public", lambda host: True), \
                mock.patch.object(sources, "is_public_ip", lambda ip: False):
            verdict, detail = sources.fetch(f"http://127.0.0.1:{port}/doc")
        self.assertEqual(verdict, "dead")
        self.assertIn("private", detail)

    def test_urllib_parses_the_host_it_connects_to(self):
        port = self.server.server_address[1]
        with mock.patch.object(sources, "is_public_ip", lambda ip: False):
            for url in (f"http://127.0.0.1:{port}\\@example.com/doc", f"http://127.0.0.1:{port}#@example.com/doc"):
                with self.subTest(url=url):
                    self.assertNotEqual(sources.fetch(url)[0], "ok")

    def test_a_proxy_gets_the_request_and_the_host_check_still_applies(self):
        with mock.patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:9"}):
            self.assertEqual(sources.fetch("http://10.0.0.1/x")[0], "bad-url")
            self.assertTrue(sources.proxied("http://example.com/"))
        self.assertFalse(sources.proxied("http://example.com/"))

    def test_through_a_proxy_an_unverifiable_host_is_refused(self):
        with mock.patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:9"}):
            verdict, detail = sources.fetch("http://intranet.invalid/wiki")
        self.assertEqual(verdict, "bad-url")
        self.assertIn("cannot confirm", detail)
        self.assertEqual(sources.fetch("http://intranet.invalid/wiki")[0], "dead")

    def test_a_redirect_to_a_private_address_is_refused(self):
        verdict, detail = sources.fetch(f"{self.base}/to-private")
        self.assertEqual(verdict, "dead")
        self.assertIn("private", detail)

    def test_nat64_addresses_are_judged_by_the_ipv4_they_carry(self):
        self.assertFalse(REAL_IS_PUBLIC_IP("64:ff9b::a00:1"))
        self.assertFalse(REAL_IS_PUBLIC_IP("64:ff9b::a9fe:a9fe"))
        self.assertFalse(REAL_IS_PUBLIC_IP("64:ff9b:1::808:808"))
        self.assertTrue(REAL_IS_PUBLIC_IP("64:ff9b::808:808"))

    def test_an_unknown_or_binary_charset_falls_back_to_utf8(self):
        for ctype in ("text/html; charset=utf8mb4", "text/plain; charset=hex", "text/plain; charset=zlib"):
            with self.subTest(ctype=ctype):
                self.assertEqual(sources.page_text("caf\u00e9 au lait".encode(), ctype), "caf\u00e9 au lait")

    def test_one_broken_page_does_not_abort_the_batch(self):
        rows = [{"claim": "a", "url": f"{self.base}/mb4", "quote": "caf\u00e9 opens"},
                {"claim": "b", "url": f"{self.base}/short", "quote": "cut short"},
                {"claim": "c", "url": f"{self.base}/doc", "quote": "default limit"}]
        self.assertEqual(self.verdicts(rows), ["ok", "dead", "ok"])

    def test_a_quote_past_the_read_limit_is_unverified_not_missing(self):
        with mock.patch.object(sources, "MAX_BYTES", 40):
            verdicts = self.verdicts([{"claim": "a", "url": f"{self.base}/plain", "quote": "line two"},
                                      {"claim": "b", "url": f"{self.base}/doc", "quote": "stops adding"},
                                      {"claim": "c", "url": f"{self.base}/plain", "quote": "line three"}])
        self.assertEqual(verdicts, ["ok", "unreadable", "quote-missing"])

    def test_a_quote_must_match_whole_words(self):
        rows = [{"claim": "a", "url": f"{self.base}/pep", "quote": "toml"},
                {"claim": "b", "url": f"{self.base}/pep", "quote": "omllib module"},
                {"claim": "c", "url": f"{self.base}/pep", "quote": "tomllib module"}]
        self.assertEqual(self.verdicts(rows), ["ok", "quote-missing", "ok"])

    def evidence(self, rows: list[dict]) -> str:
        return str(write(tree(self) / "evidence.jsonl", "\n".join(json.dumps(r) for r in rows) + "\n"))

    def run_cli(self, *files: str) -> tuple[int, str]:
        out = io.StringIO()
        env = {k: v for k, v in os.environ.items() if k != "CODEX_SANDBOX_NETWORK_DISABLED"}
        with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = sources.cli(types.SimpleNamespace(files=list(files)))
        return code, out.getvalue()

    def verdicts(self, rows: list[dict]) -> list[str]:
        claims = sources.load([self.evidence(rows)])
        return [c.verdict for c in sources.check(claims)]

    def test_quote_found_through_markup_entities_and_curly_quotes(self):
        rows = [{"claim": "c", "url": f"{self.base}/doc", "quote": 'default limit is 32 KiB and Codex "stops adding" files'}]
        self.assertEqual(self.verdicts(rows), ["ok"])

    def test_quote_found_across_tag_spacing_and_copied_backticks(self):
        rows = [{"claim": "c", "url": f"{self.base}/pep", "quote": "Python-Version: 3.11"},
                {"claim": "c", "url": f"{self.base}/pep", "quote": "The `tomllib` module parses TOML."},
                {"claim": "c", "url": f"{self.base}/pep", "quote": "Python-Version: 3.12"},
                {"claim": "c", "url": f"{self.base}/pep", "quote": "... \u2014 `` _"}]
        self.assertEqual(self.verdicts(rows), ["ok", "ok", "quote-missing", "no-quote"])

    def test_evidence_can_come_from_stdin(self):
        row = json.dumps({"claim": "c", "url": f"{self.base}/doc", "quote": "default limit"})
        with mock.patch.object(sources.sys, "stdin", io.StringIO(row + "\n")):
            code, out = self.run_cli("-")
        self.assertEqual(code, 0)
        self.assertIn("-:1", out)

    def test_script_text_is_not_page_text(self):
        self.assertEqual(self.verdicts([{"claim": "c", "url": f"{self.base}/doc", "quote": "not text"}]), ["quote-missing"])

    def test_parser_assertion_keeps_only_the_text_collected_so_far(self):
        def feed(parser, data):
            parser.parts.append("default limit ")
            raise AssertionError("expected name token")

        with mock.patch.object(sources._Text, "feed", feed):
            text = sources.page_text(b"<script>hidden</script><![\x00", "text/html")
        self.assertEqual(text, "default limit")

    def test_every_outcome(self):
        rows = [
            {"claim": "a", "url": f"{self.base}/plain", "quote": "line one line two"},
            {"claim": "b", "url": f"{self.base}/blocked", "quote": "x"},
            {"claim": "c", "url": f"{self.base}/gone", "quote": "x"},
            {"claim": "d", "url": f"{self.base}/pdf", "quote": "x"},
            {"claim": "e", "url": f"{self.base}/moved", "quote": "default limit"},
            {"claim": "f", "url": "file:///etc/passwd", "quote": "root"},
            {"claim": "g", "url": f"{self.base}/doc"},
        ]
        self.assertEqual(self.verdicts(rows), ["ok", "blocked", "dead", "unreadable", "ok", "bad-url", "no-quote"])

    def test_each_url_is_fetched_once(self):
        rows = [{"claim": str(i), "url": f"{self.base}/doc", "quote": "default limit"} for i in range(5)]
        with mock.patch.object(sources, "fetch", wraps=sources.fetch) as fetch:
            sources.check(sources.load([self.evidence(rows)]))
        self.assertEqual(fetch.call_count, 1)

    def test_cli_exit_codes_and_summary(self):
        good = self.evidence([{"claim": "c", "url": f"{self.base}/doc", "quote": "default limit"}])
        self.assertEqual(self.run_cli(good)[0], 0)
        bad = self.evidence([{"claim": "c", "url": f"{self.base}/gone", "quote": "x"}])
        code, out = self.run_cli(good, bad)
        self.assertEqual(code, 1)
        self.assertIn("2 claims: 1 ok", out)

    def test_malformed_lines_are_reported_not_fatal(self):
        path = write(tree(self) / "e.jsonl", '{"claim": "c", "url": "u"\nnot json\n\n')
        claims = sources.load([str(path)])
        self.assertEqual([c.verdict for c in claims], ["malformed", "malformed"])
        self.assertIn("e.jsonl:2", claims[1].where)

    def test_patterns_are_expanded_here_not_by_the_shell(self):
        root = tree(self)
        for name in ("evidence-a.jsonl", "evidence-b.jsonl", "notes.txt"):
            write(root / name, "")
        self.assertEqual([Path(f).name for f in sources.expand([str(root / "evidence-*.jsonl")])], ["evidence-a.jsonl", "evidence-b.jsonl"])
        self.assertEqual(sources.expand(["plain.jsonl"]), ["plain.jsonl"])
        with self.assertRaises(FileNotFoundError):
            sources.load([str(root / "evidnce-*.jsonl")])

    def test_a_file_named_with_brackets_is_opened_as_is(self):
        root = tree(self)
        write(root / "report [final].jsonl", "")
        self.assertEqual(sources.expand([str(root / "report [final].jsonl")]), [str(root / "report [final].jsonl")])
        with self.assertRaises(FileNotFoundError):
            sources.expand([str(root / "report [draft].jsonl")])

    def test_sandboxed_network_stops_early(self):
        with mock.patch.dict(os.environ, {"CODEX_SANDBOX_NETWORK_DISABLED": "1"}):
            out = io.StringIO()
            with contextlib.redirect_stderr(out):
                code = sources.cli(types.SimpleNamespace(files=[self.evidence([{"claim": "c", "url": "https://x", "quote": "q"}])]))
        self.assertEqual(code, 2)
        self.assertIn("network", out.getvalue())


if __name__ == "__main__":
    unittest.main()
