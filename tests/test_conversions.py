"""End-to-end conversion tests: every source fixture to every target it offers.

Run with:  python -m unittest discover -s tests -v
Needs ffmpeg, LibreOffice and pandoc on PATH (tests for a missing tool are skipped).
"""
import io
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw  # noqa: E402

from app import app  # noqa: E402
from converter import REGISTRY, find_libreoffice  # noqa: E402

HAS_FFMPEG = bool(shutil.which("ffmpeg"))
HAS_LO = bool(find_libreoffice())
HAS_PANDOC = bool(shutil.which("pandoc"))

# Expected leading bytes of a few outputs, to prove we got the real format.
SIGNATURES = {
    "pdf": b"%PDF", "png": b"\x89PNG", "jpg": b"\xff\xd8\xff", "gif": b"GIF8",
    "docx": b"PK", "xlsx": b"PK", "pptx": b"PK", "odt": b"PK", "ods": b"PK", "odp": b"PK",
    "epub": b"PK", "flac": b"fLaC", "ogg": b"OggS", "opus": b"OggS", "wav": b"RIFF",
    "avi": b"RIFF", "webm": b"\x1a\x45\xdf\xa3", "mkv": b"\x1a\x45\xdf\xa3", "bmp": b"BM",
    "rtf": b"{\\rtf",
}


def make_fixtures(d: Path) -> dict:
    fx = {}
    im = Image.new("RGBA", (300, 200), (0, 0, 0, 0))
    ImageDraw.Draw(im).ellipse((20, 20, 280, 180), fill=(79, 70, 229, 255))
    im.save(d / "logo.png")
    fx["png"] = d / "logo.png"
    im.convert("RGB").save(d / "photo.jpg")
    fx["jpg"] = d / "photo.jpg"
    frames = [Image.new("RGB", (64, 64), c) for c in ("red", "green", "blue")]
    frames[0].save(d / "anim.gif", save_all=True, append_images=frames[1:], duration=200, loop=0)
    fx["gif"] = d / "anim.gif"

    (d / "notes.md").write_text("# Hello\n\nSome **bold** text.\n\n- one\n- two\n")
    fx["md"] = d / "notes.md"
    (d / "plain.txt").write_text("Plain text.\nSecond line.\n")
    fx["txt"] = d / "plain.txt"
    (d / "page.html").write_text("<html><body><h1>Title</h1><p>Hello <b>world</b></p></body></html>")
    fx["html"] = d / "page.html"
    (d / "scores.csv").write_text("name,score\nAsha,9\nRavi,7\n")
    fx["csv"] = d / "scores.csv"

    if HAS_FFMPEG:
        run = lambda *a: subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *a], check=True)
        run("-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(d / "tone.wav"))
        fx["wav"] = d / "tone.wav"
        run("-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=12", "-f", "lavfi", "-i",
            "sine=frequency=440:duration=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-shortest", str(d / "clip.mp4"))
        fx["mp4"] = d / "clip.mp4"
    if HAS_LO:
        subprocess.run(["soffice", "--headless", "--convert-to", "docx:MS Word 2007 XML", "--outdir", str(d), str(d / "plain.txt")], check=True, capture_output=True)
        fx["docx"] = d / "plain.docx"
        subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", str(d), str(d / "plain.docx")], check=True, capture_output=True)
        fx["pdf"] = d / "plain.pdf"
        subprocess.run(["soffice", "--headless", "--convert-to", "xlsx:Calc MS Excel 2007 XML", "--outdir", str(d), str(d / "scores.csv")], check=True, capture_output=True)
        fx["xlsx"] = d / "scores.xlsx"
        subprocess.run(["soffice", "--headless", "--infilter=impress_pdf_import", "--convert-to",
                        "pptx:Impress MS PowerPoint 2007 XML", "--outdir", str(d), str(d / "plain.pdf")], check=True, capture_output=True)
        fx["pptx"] = d / "plain.pptx"
    return fx


class ConversionMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.fx = make_fixtures(cls.tmp)
        cls.client = app.test_client()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def convert(self, path: Path, target: str, **form):
        data = {"target": target, "files": [(io.BytesIO(path.read_bytes()), path.name)], **form}
        return self.client.post("/api/convert", data=data, content_type="multipart/form-data")

    def check_output(self, resp, target):
        self.assertEqual(resp.status_code, 200, resp.get_data()[:300] if resp.status_code != 200 else "")
        body = resp.get_data()
        self.assertGreater(len(body), 0)
        name = resp.headers.get("Content-Disposition", "")
        if ".zip" in name:  # multi-page output (e.g. PDF -> PNG pages)
            with zipfile.ZipFile(io.BytesIO(body)) as zf:
                body = zf.read(zf.namelist()[0])
        sig = SIGNATURES.get(target)
        if sig:
            self.assertTrue(body.startswith(sig), f"{target} output starts with {body[:8]!r}")

    def test_every_fixture_to_every_target(self):
        for ext, path in self.fx.items():
            for target in REGISTRY[ext]["targets"]:
                with self.subTest(src=ext, target=target):
                    self.check_output(self.convert(path, target), target)

    def test_animated_gif_stays_animated(self):
        resp = self.convert(self.fx["gif"], "webp")
        self.assertEqual(resp.status_code, 200)
        self.assertGreater(Image.open(io.BytesIO(resp.get_data())).n_frames, 1)

    def test_transparent_png_to_jpg_gets_white_background(self):
        resp = self.convert(self.fx["png"], "jpg")
        img = Image.open(io.BytesIO(resp.get_data()))
        self.assertEqual(img.getpixel((2, 2)), (255, 255, 255))

    def test_image_resize_option(self):
        resp = self.client.post("/api/convert", data={"target": "webp", "max_size": "100",
                                "files": [(io.BytesIO(self.fx["png"].read_bytes()), "logo.png")]},
                                content_type="multipart/form-data")
        self.assertEqual(max(Image.open(io.BytesIO(resp.get_data())).size), 100)

    def test_multiple_images_combine_into_one_pdf(self):
        files = [(io.BytesIO(self.fx["png"].read_bytes()), "a.png"), (io.BytesIO(self.fx["jpg"].read_bytes()), "b.jpg")]
        resp = self.client.post("/api/convert", data={"target": "pdf", "combine_pdf": "true", "files": files},
                                content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 200)
        import pymupdf
        self.assertEqual(len(pymupdf.open(stream=resp.get_data(), filetype="pdf")), 2)

    def test_multiple_files_come_back_zipped(self):
        files = [(io.BytesIO(self.fx["png"].read_bytes()), "a.png"), (io.BytesIO(self.fx["png"].read_bytes()), "a.png")]
        resp = self.client.post("/api/convert", data={"target": "jpg", "files": files}, content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(resp.get_data())) as zf:
            self.assertEqual(len(zf.namelist()), 2)  # duplicate names don't overwrite each other

    @unittest.skipUnless(HAS_FFMPEG, "ffmpeg not installed")
    def test_audio_only_file_cannot_become_video(self):
        self.assertNotIn("mp4", REGISTRY["wav"]["targets"])

    def test_renamed_file_is_rejected(self):
        resp = self.client.post("/api/convert", data={"target": "jpg", "files": [(io.BytesIO(b"not a png"), "fake.png")]},
                                content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 400)

    def test_unsupported_pair_is_rejected(self):
        resp = self.convert(self.fx["png"], "mp3")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("can't convert", resp.get_json()["error"])

    def test_wrong_category_is_rejected(self):
        resp = self.client.post("/api/convert", data={"target": "mp3", "category": "audio",
                                "files": [(io.BytesIO(self.fx["png"].read_bytes()), "logo.png")]},
                                content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("isn't an audio file", resp.get_json()["error"])

    def test_formats_endpoint(self):
        data = self.client.get("/api/formats").get_json()
        self.assertIn("mp4", data["formats"])
        self.assertIn("mp3", data["formats"]["mp4"]["targets"])


if __name__ == "__main__":
    unittest.main()
