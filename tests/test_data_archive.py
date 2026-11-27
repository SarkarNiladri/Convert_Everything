"""Tests for the Data and Archive converters.

Both converters are pure Python, so these tests need no system tools.
Run with:  python -m unittest discover -s tests -v
"""
import csv
import io
import json
import shutil
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import openpyxl  # noqa: E402
import py7zr  # noqa: E402
import yaml  # noqa: E402

import archive_converter  # noqa: E402
from app import app  # noqa: E402
from converter import CATEGORIES  # noqa: E402

ROWS = [{"name": "Asha", "score": 9, "code": "007"}, {"name": "Ravi", "score": 7.5, "code": "A1"}]
FILES = {"proj/readme.txt": b"hello\n", "proj/src/main.py": b"print('hi')\n", "proj/data/bin.dat": bytes(range(256))}


def make_data_fixtures(d: Path) -> dict:
    fx = {}
    (d / "people.csv").write_text("name,score,code\nAsha,9,007\nRavi,7.5,A1\n")
    fx["csv"] = d / "people.csv"
    (d / "people.json").write_text(json.dumps(ROWS))
    fx["json"] = d / "people.json"
    (d / "people.yaml").write_text(yaml.safe_dump(ROWS))
    fx["yaml"] = d / "people.yaml"
    (d / "config.yml").write_text("server:\n  host: localhost\n  port: 8080\n")
    fx["yml"] = d / "config.yml"
    (d / "people.xml").write_text("<?xml version='1.0'?><people><person id='1'><name>Asha</name><code>007</code></person>"
                                  "<person id='2'><name>Ravi</name><code>A1</code></person></people>")
    fx["xml"] = d / "people.xml"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "People"
    for row in [["name", "score", "code"], ["Asha", 9, "007"], ["Ravi", 7.5, "A1"]]:
        ws.append(row)
    wb.save(d / "people.xlsx")
    fx["xlsx"] = d / "people.xlsx"
    (d / "notes.md").write_text("# Notes\n\nSome **bold** text.\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n```\ncode\n```\n")
    fx["md"] = d / "notes.md"
    return fx


def make_archive_fixtures(d: Path) -> dict:
    src = d / "tree"
    for name, data in FILES.items():
        (src / name).parent.mkdir(parents=True, exist_ok=True)
        (src / name).write_bytes(data)
    (src / "proj" / "empty").mkdir()
    fx = {}
    with zipfile.ZipFile(d / "bundle.zip", "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in FILES.items():
            zf.writestr(name, data)
    fx["zip"] = d / "bundle.zip"
    for ext, mode in (("tar", "w"), ("tar.gz", "w:gz"), ("tgz", "w:gz")):
        with tarfile.open(d / f"bundle.{ext}", mode) as tf:
            tf.add(src / "proj", arcname="proj")
        fx[ext] = d / f"bundle.{ext}"
    with py7zr.SevenZipFile(d / "bundle.7z", "w") as z:
        z.writeall(src / "proj", arcname="proj")
    fx["7z"] = d / "bundle.7z"
    return fx


def read_archive(data: bytes, target: str) -> dict:
    """{name: bytes} of every regular file in an archive."""
    out = {}
    if target == "zip":
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                if not info.is_dir():
                    out[info.filename] = zf.read(info)
    elif target in ("tar", "tar.gz"):
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
            for m in tf.getmembers():
                if m.isreg():
                    out[m.name] = tf.extractfile(m).read()
    elif target == "7z":
        tmp = Path(tempfile.mkdtemp())
        try:
            with py7zr.SevenZipFile(io.BytesIO(data), "r") as z:
                z.extractall(path=tmp)
            for p in tmp.rglob("*"):
                if p.is_file():
                    out[p.relative_to(tmp).as_posix()] = p.read_bytes()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return out


def load_output(data: bytes, target: str):
    """Parse a data output back into Python, proving it is valid."""
    if target == "json":
        return json.loads(data)
    if target == "yaml":
        return yaml.safe_load(data)
    if target == "csv":
        return list(csv.DictReader(io.StringIO(data.decode("utf-8"))))
    if target == "xlsx":
        wb = openpyxl.load_workbook(io.BytesIO(data))
        return {ws.title: [list(r) for r in ws.iter_rows(values_only=True)] for ws in wb.worksheets}
    if target == "xml":
        from defusedxml import ElementTree
        return ElementTree.fromstring(data)
    if target == "html":
        text = data.decode("utf-8")
        assert text.startswith("<!DOCTYPE html>"), text[:40]
        return text
    if target == "pdf":
        import pymupdf
        return pymupdf.open(stream=data, filetype="pdf")[0].get_text()
    raise AssertionError(target)


class ApiMixin:
    client = app.test_client()

    def post(self, files, target, category=None):
        data = {"target": target, "files": [(io.BytesIO(b), n) for n, b in files]}
        if category:
            data["category"] = category
        return self.client.post("/api/convert", data=data, content_type="multipart/form-data")

    def convert(self, path: Path, target: str, category: str | None = None):
        return self.post([(path.name, path.read_bytes())], target, category)

    def ok(self, resp):
        self.assertEqual(resp.status_code, 200, resp.get_data()[:300])
        return resp.get_data()


class DataConverterTests(ApiMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.fx = make_data_fixtures(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_every_fixture_to_every_target(self):
        for ext, path in self.fx.items():
            for target in CATEGORIES["data"][ext]:
                with self.subTest(src=ext, target=target):
                    load_output(self.ok(self.convert(path, target, "data")), target)

    def test_csv_to_json_infers_numbers_but_keeps_codes(self):
        rows = json.loads(self.ok(self.convert(self.fx["csv"], "json", "data")))
        self.assertEqual(rows, ROWS)  # 9 and 7.5 are numbers, "007" stays text

    def test_round_trip_through_every_format(self):
        for fmt in ("json", "xml", "yaml", "xlsx"):
            with self.subTest(via=fmt):
                mid = self.ok(self.convert(self.fx["csv"], fmt, "data"))
                back_fmt = "yaml" if fmt == "json" else "json"
                data = load_output(self.ok(self.post([(f"people.{fmt}", mid)], back_fmt, "data")), back_fmt)
                if fmt == "xml":  # <rows><row>..</row></rows>
                    data = data["rows"]["row"]
                self.assertEqual(data, ROWS)

    def test_xml_attributes_and_repeated_elements(self):
        data = json.loads(self.ok(self.convert(self.fx["xml"], "json", "data")))
        people = data["people"]["person"]
        self.assertEqual(people[0], {"@id": 1, "name": "Asha", "code": "007"})
        rows = load_output(self.ok(self.convert(self.fx["xml"], "csv", "data")), "csv")
        self.assertEqual([r["name"] for r in rows], ["Asha", "Ravi"])
        self.assertEqual(rows[0]["@id"], "1")

    def test_nested_json_flattens_for_csv_and_excel(self):
        doc = {"team": [{"name": "A", "address": {"city": "Pune"}, "tags": ["x", "y"]}, {"name": "B", "note": "=1+1"}]}
        resp = self.post([("team.json", json.dumps(doc).encode())], "csv", "data")
        rows = load_output(self.ok(resp), "csv")
        self.assertEqual(rows[0]["address.city"], "Pune")
        self.assertEqual(json.loads(rows[0]["tags"]), ["x", "y"])
        wb = openpyxl.load_workbook(io.BytesIO(self.ok(self.post([("team.json", json.dumps(doc).encode())], "xlsx", "data"))))
        ws = wb["team"]
        note = ws.cell(row=3, column=4)
        self.assertEqual(note.value, "=1+1")
        self.assertEqual(note.data_type, "s")  # stored as text, never a live formula

    def test_multi_sheet_workbook(self):
        wb = openpyxl.Workbook()
        wb.active.title = "A"
        wb.active.append(["x"])
        wb.active.append([1])
        b = wb.create_sheet("B")
        b.append(["y"])
        b.append([2])
        buf = io.BytesIO()
        wb.save(buf)
        resp = self.post([("book.xlsx", buf.getvalue())], "csv", "data")
        with zipfile.ZipFile(io.BytesIO(self.ok(resp))) as zf:
            self.assertEqual(sorted(zf.namelist()), ["book_A.csv", "book_B.csv"])
        data = json.loads(self.ok(self.post([("book.xlsx", buf.getvalue())], "json", "data")))
        self.assertEqual(data, {"A": [{"x": 1}], "B": [{"y": 2}]})
        # ...and back: JSON with one list per key becomes one sheet per key.
        sheets = load_output(self.ok(self.post([("book.json", json.dumps(data).encode())], "xlsx", "data")), "xlsx")
        self.assertEqual(sheets, {"A": [["x"], [1]], "B": [["y"], [2]]})

    def test_semicolon_csv_is_detected(self):
        resp = self.post([("eu.csv", "name;price\nTea;2.5\n".encode())], "json", "data")
        self.assertEqual(json.loads(self.ok(resp)), [{"name": "Tea", "price": 2.5}])

    def test_markdown_to_html_and_pdf(self):
        html = load_output(self.ok(self.convert(self.fx["md"], "html", "data")), "html")
        self.assertIn("<strong>bold</strong>", html)
        self.assertIn("<table>", html)
        text = load_output(self.ok(self.convert(self.fx["md"], "pdf", "data")), "pdf")
        self.assertIn("Notes", text)
        self.assertIn("bold", text)

    def test_xml_entities_are_refused(self):
        bomb = b'<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><l>&b;</l>'
        resp = self.post([("bomb.xml", bomb)], "json", "data")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("aren't allowed", resp.get_json()["error"])

    def test_invalid_json_gives_clear_error(self):
        resp = self.post([("bad.json", b'{"a": ')], "csv", "data")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("isn't valid JSON", resp.get_json()["error"])

    def test_csv_without_category_uses_data_converter_for_json(self):
        rows = json.loads(self.ok(self.convert(self.fx["csv"], "json")))
        self.assertEqual(rows, ROWS)

    def test_archive_in_data_converter_is_rejected(self):
        resp = self.post([("a.zip", b"PK\x05\x06" + b"\0" * 18)], "json", "data")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("isn't a data file", resp.get_json()["error"])


class ArchiveConverterTests(ApiMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.fx = make_archive_fixtures(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_every_archive_to_every_target_keeps_contents(self):
        for ext, path in self.fx.items():
            for target in CATEGORIES["archive"][ext]:
                with self.subTest(src=ext, target=target):
                    resp = self.convert(path, target, "archive")
                    body = self.ok(resp)
                    self.assertIn(f'bundle.{target}', resp.headers["Content-Disposition"])
                    self.assertEqual(read_archive(body, target), FILES)

    def test_empty_folders_are_kept(self):
        body = self.ok(self.convert(self.fx["tar"], "zip", "archive"))
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            self.assertIn("proj/empty/", zf.namelist())

    def test_tar_owner_is_not_leaked(self):
        body = self.ok(self.convert(self.fx["zip"], "tar", "archive"))
        with tarfile.open(fileobj=io.BytesIO(body)) as tf:
            for m in tf.getmembers():
                self.assertEqual((m.uid, m.gid, m.uname, m.gname), (0, 0, "", ""))

    def test_path_traversal_and_links_are_skipped(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("../../escape.txt", "x")
            zf.writestr("/abs/root.txt", "y")
            zf.writestr("ok.txt", "ok")
            link = zipfile.ZipInfo("link")
            link.external_attr = 0o120777 << 16  # symlink
            zf.writestr(link, "/etc/passwd")
        body = self.ok(self.post([("evil.zip", buf.getvalue())], "tar", "archive"))
        self.assertEqual(read_archive(body, "tar"), {"abs/root.txt": b"y", "ok.txt": b"ok"})

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            for name in ("../up.txt", "fine.txt"):
                info = tarfile.TarInfo(name)
                info.size = 1
                tf.addfile(info, io.BytesIO(b"a"))
            for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                info = tarfile.TarInfo(f"link{kind.decode()}")
                info.type, info.linkname = kind, "/etc/passwd"
                tf.addfile(info)
        body = self.ok(self.post([("evil.tar", buf.getvalue())], "zip", "archive"))
        self.assertEqual(read_archive(body, "zip"), {"fine.txt": b"a"})

    def test_unpacked_size_limit(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("zeros.bin", b"\0" * 100_000)
        with mock.patch.object(archive_converter, "MAX_UNPACKED_BYTES", 10_000):
            resp = self.post([("bomb.zip", buf.getvalue())], "tar", "archive")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("over the limit", resp.get_json()["error"])

    def test_entry_count_limit(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for i in range(20):
                zf.writestr(f"f{i}.txt", "x")
        with mock.patch.object(archive_converter, "MAX_ENTRIES", 10):
            resp = self.post([("many.zip", buf.getvalue())], "7z", "archive")
        self.assertEqual(resp.status_code, 400)

    def test_password_protected_7z_is_refused(self):
        tmp = self.tmp / "locked.7z"
        with py7zr.SevenZipFile(tmp, "w", password="secret") as z:
            z.writestr(b"secret", "s.txt")
        resp = self.convert(tmp, "zip", "archive")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("password", resp.get_json()["error"])

    def test_not_an_archive(self):
        resp = self.post([("fake.zip", b"PK not really a zip")], "tar", "archive")
        self.assertEqual(resp.status_code, 400)
        resp = self.post([("fake.7z", b"hello")], "zip", "archive")
        self.assertEqual(resp.status_code, 400)


class RegistryTests(ApiMixin, unittest.TestCase):
    def test_formats_endpoint_lists_new_converters(self):
        data = self.client.get("/api/formats").get_json()
        cats = data["categories"]
        self.assertIn("json", cats["data"]["csv"]["targets"])
        self.assertEqual(cats["data"]["md"]["targets"], ["html", "pdf"])
        self.assertIn("7z", cats["archive"]["tar.gz"]["targets"])
        self.assertNotIn("tar.gz", cats["archive"]["tgz"]["targets"])
        # Existing converters are unchanged.
        self.assertEqual(data["formats"]["csv"]["category"], "spreadsheet")
        self.assertIn("csv", cats["spreadsheet"])
        self.assertEqual(data["labels"]["tar.gz"], "TAR.GZ (gzip)")


if __name__ == "__main__":
    unittest.main()
