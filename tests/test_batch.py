"""Tests for batch upload: the per-file API contract the batch UI relies on, the
browser-side ZIP writer (static/zipwriter.js, run under Node), and the page wiring.

Run with:  python -m unittest discover -s tests -v
The ZIP writer tests need Node 18+ on PATH and are skipped without it. Nothing here needs
ffmpeg, LibreOffice or pandoc.
"""
import base64
import io
import json
import os
import random
import shutil
import subprocess
import sys
import unittest
import zipfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import MAX_FILES, app  # noqa: E402

ZIPWRITER = ROOT / "static" / "zipwriter.js"
NODE = shutil.which("node")

# Reads [{name, data(base64), date?}] on stdin and prints the zip as base64.
NODE_ZIP = r"""
const { makeZip } = require(process.argv[1]);
let input = '';
process.stdin.on('data', c => input += c).on('end', async () => {
  try {
    const entries = JSON.parse(input).map(e => ({
      name: e.name, blob: new Blob([Buffer.from(e.data, 'base64')]), date: e.date ? new Date(e.date) : undefined,
    }));
    const blob = await makeZip(entries);
    process.stdout.write(Buffer.from(await blob.arrayBuffer()).toString('base64'));
  } catch (err) { process.stderr.write(String(err.message)); process.exit(2); }
});
"""


def node_zip(entries: list[dict]) -> bytes:
    payload = [{"name": n, "data": base64.b64encode(d).decode(), **({"date": t} if t else {})}
               for n, d, t in ((e[0], e[1], e[2] if len(e) > 2 else None) for e in entries)]
    proc = subprocess.run([NODE, "-e", NODE_ZIP, str(ZIPWRITER)], input=json.dumps(payload),
                          capture_output=True, text=True, timeout=60)
    if proc.returncode:
        raise RuntimeError(proc.stderr)
    return base64.b64decode(proc.stdout)


def node_eval(expr: str):
    script = f"const z = require(process.argv[1]); process.stdout.write(JSON.stringify({expr}));"
    proc = subprocess.run([NODE, "-e", script, str(ZIPWRITER)], capture_output=True, text=True, timeout=60, check=True)
    return json.loads(proc.stdout)


class BatchApiTests(unittest.TestCase):
    """The batch UI sends one /api/convert request per file and reads the file name from the response."""

    def setUp(self):
        self.client = app.test_client()

    def convert(self, name: str, data: bytes, target: str, category: str):
        return self.client.post("/api/convert", data={
            "files": [(io.BytesIO(data), name)], "target": target, "category": category,
        }, content_type="multipart/form-data")

    def test_formats_reports_limits(self):
        limits = self.client.get("/api/formats").get_json()["limits"]
        self.assertEqual(limits["max_files"], MAX_FILES)
        self.assertEqual(limits["max_upload_mb"], app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024))

    def test_each_file_converts_on_its_own_with_its_name(self):
        files = {"people.csv": b"name,score\nAsha,9\n", "more people.csv": b"name,score\nRavi,7\n"}
        for name, data in files.items():
            res = self.convert(name, data, "json", "data")
            self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
            self.assertRegex(res.headers["Content-Disposition"], r"attachment; filename.*\.json")
            self.assertIsInstance(json.loads(res.data), list)
            res.close()

    def test_one_bad_file_fails_alone(self):
        good = self.convert("ok.csv", b"a,b\n1,2\n", "yaml", "data")
        bad = self.convert("broken.json", b"{not json", "yaml", "data")
        self.assertEqual(good.status_code, 200)
        good.close()
        self.assertEqual(bad.status_code, 400)
        self.assertIn("error", bad.get_json())

    def test_wrong_category_is_refused(self):
        res = self.convert("photo.png", b"\x89PNG\r\n\x1a\n", "json", "data")
        self.assertEqual(res.status_code, 400)

    def test_upload_limit_still_applies_per_request(self):
        old = app.config["MAX_CONTENT_LENGTH"]
        app.config["MAX_CONTENT_LENGTH"] = 1024
        try:
            res = self.convert("big.csv", b"a,b\n" + b"1,2\n" * 1000, "json", "data")
            self.assertEqual(res.status_code, 413)
            self.assertIn("limit", res.get_json()["error"])
        finally:
            app.config["MAX_CONTENT_LENGTH"] = old

    def test_too_many_files_refused(self):
        files = [(io.BytesIO(b"a\n1\n"), f"f{i}.csv") for i in range(MAX_FILES + 1)]
        res = self.client.post("/api/convert", data={"files": files, "target": "json", "category": "data"},
                               content_type="multipart/form-data")
        self.assertEqual(res.status_code, 400)


class PageTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_zipwriter_is_served(self):
        res = self.client.get("/static/zipwriter.js")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"makeZip", res.data)
        res.close()

    def test_page_has_batch_progress_and_history(self):
        res = self.client.get("/")
        html = res.get_data(as_text=True)
        res.close()
        for needle in ('src="/static/zipwriter.js"', 'id="cancel"', 'id="historyList"', 'id="clearHistory"',
                       "localStorage", "upload.onprogress", "ZipWriter.makeZip"):
            self.assertIn(needle, html)


@unittest.skipUnless(NODE, "node is not installed")
class ZipWriterTests(unittest.TestCase):
    def test_crc32_matches_zlib(self):
        rnd = random.Random(7)
        for size in (0, 1, 255, 4096):
            data = bytes(rnd.getrandbits(8) for _ in range(size))
            got = node_eval(f"z.crc32(Uint8Array.from({list(data)}))")
            self.assertEqual(got, zlib.crc32(data))

    def test_zip_opens_and_checks_out(self):
        rnd = random.Random(1)
        binary = bytes(rnd.getrandbits(8) for _ in range(70000))
        entries = [("report.pdf", b"%PDF-1.7 fake"), ("photo.jpg", binary), ("empty.txt", b""),
                   ("résumé 简历.docx", b"PK\x03\x04 unicode name"), ("report.pdf", b"second report")]
        data = node_zip(entries)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            self.assertIsNone(zf.testzip())  # every CRC matches
            names = zf.namelist()
            self.assertEqual(names, ["report.pdf", "photo.jpg", "empty.txt", "résumé 简历.docx", "report (2).pdf"])
            self.assertEqual(zf.read("photo.jpg"), binary)
            self.assertEqual(zf.read("report (2).pdf"), b"second report")
            self.assertEqual(zf.read("empty.txt"), b"")

    def test_dates_are_kept(self):
        data = node_zip([("a.txt", b"x", "2024-03-05T14:30:20")])
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            self.assertEqual(zf.getinfo("a.txt").date_time, (2024, 3, 5, 14, 30, 20))

    def test_unique_names(self):
        got = node_eval("z.uniqueNames(['a.pdf', 'A.pdf', 'b.tar.gz', 'b.tar.gz', 'noext', 'noext', '../x/y.txt', ''])")
        self.assertEqual(got, ["a.pdf", "A (2).pdf", "b.tar.gz", "b (2).tar.gz", "noext", "noext (2)", "_x_y.txt", "file"])

    def test_batch_end_to_end(self):
        """Convert several files one request at a time, then zip the results like the browser does."""
        client = app.test_client()
        sources = {"a.csv": b"n\n1\n", "b.yaml": b"- n: 2\n", "c.json": b'[{"n": 3}]'}
        outputs = []
        for name, data in sources.items():
            res = client.post("/api/convert", data={"files": [(io.BytesIO(data), name)], "target": "xml", "category": "data"},
                              content_type="multipart/form-data")
            self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
            fname = res.headers["Content-Disposition"].split("filename=")[-1].strip('"')
            outputs.append((fname, res.data))
            res.close()
        with zipfile.ZipFile(io.BytesIO(node_zip(outputs))) as zf:
            self.assertIsNone(zf.testzip())
            self.assertEqual(sorted(zf.namelist()), ["a.xml", "b.xml", "c.xml"])
            for name in zf.namelist():
                self.assertTrue(zf.read(name).lstrip().startswith(b"<"))


if __name__ == "__main__":
    unittest.main()
