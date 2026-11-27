"""Data converters: CSV, JSON, XML, YAML and Excel (XLSX) to each other,
plus Markdown to HTML and Markdown to PDF.

Everything here is pure Python (openpyxl, PyYAML, defusedxml, Markdown) plus
PyMuPDF, which the app already ships, so no system tools are needed.

Every tabular format is read into one plain Python structure (dicts, lists and
scalars) and written back out from it:

* CSV and Excel become a list of row dicts (one list per sheet for Excel).
* JSON and YAML are loaded as they are.
* XML becomes nested dicts: attributes are "@name", mixed text is "#text",
  and repeated child elements become lists.

Writing to CSV or Excel finds the table inside the structure, flattens nested
objects into dotted column names ("address.city") and stores lists as JSON text.
"""
import csv
import datetime as dt
import html
import json
import re
from pathlib import Path
from xml.etree import ElementTree as ET

from errors import ConversionError

TABULAR = ["csv", "json", "xml", "yaml", "xlsx"]
DATA_IN = TABULAR + ["yml", "md"]
MARKDOWN_OUT = ["html", "pdf"]


def targets_for(ext: str) -> list[str]:
    if ext == "md":
        return list(MARKDOWN_OUT)
    same = "yaml" if ext == "yml" else ext
    return [t for t in TABULAR if t != same]


# ---------- Small helpers ----------
def _read_text(src: Path) -> str:
    raw = src.read_bytes()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("latin-1")  # never fails; good enough for old Excel CSV exports


_INT = re.compile(r"-?(0|[1-9]\d{0,15})")
_FLOAT = re.compile(r"-?(0|[1-9]\d*)\.\d+([eE][+-]?\d+)?")


def _infer(text: str):
    """Turn text from CSV/XML into a number or boolean when it clearly is one.
    Values like "007" or "1e5" stay text so IDs and codes are never mangled."""
    if _INT.fullmatch(text):
        return int(text)
    if _FLOAT.fullmatch(text):
        return float(text)
    if text in ("true", "false"):
        return text == "true"
    return text


def _plain(value):
    """Make a value safe for JSON/YAML/XML output."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _scalar_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return json.dumps(_plain(value), ensure_ascii=False)
    return str(value)


def _unique_headers(raw) -> list[str]:
    headers, seen = [], {}
    for i, h in enumerate(raw, start=1):
        name = _scalar_text(h).strip() or f"column_{i}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 1
        headers.append(name)
    return headers


def _rows_to_records(rows: list[list]) -> list[dict]:
    rows = [r for r in rows if any(v not in (None, "") for v in r)]
    if not rows:
        return []
    width = max(len(r) for r in rows)
    headers = _unique_headers(list(rows[0]) + [None] * (width - len(rows[0])))
    return [{h: (r[i] if i < len(r) else None) for i, h in enumerate(headers)} for r in rows[1:]]


# ---------- Readers ----------
def read_csv(src: Path):
    text = _read_text(src)
    sample = text[:20000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(text.splitlines(), dialect))
    records = _rows_to_records(rows)
    return [{k: (_infer(v) if isinstance(v, str) else v) for k, v in r.items()} for r in records]


def read_json(src: Path):
    try:
        return json.loads(_read_text(src))
    except json.JSONDecodeError as exc:
        raise ConversionError(f"{src.name} isn't valid JSON (line {exc.lineno}: {exc.msg}).")


def read_yaml(src: Path):
    import yaml
    try:
        docs = list(yaml.safe_load_all(_read_text(src)))
    except yaml.YAMLError as exc:
        raise ConversionError(f"{src.name} isn't valid YAML ({str(exc).splitlines()[0]}).")
    docs = [d for d in docs if d is not None]
    if not docs:
        raise ConversionError(f"{src.name} has no data in it.")
    return docs[0] if len(docs) == 1 else docs


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]  # drop "{namespace}"


def _element_to_obj(el):
    obj = {}
    for k, v in el.attrib.items():
        obj["@" + _local(k)] = _infer(v)
    for child in el:
        if not isinstance(child.tag, str):  # comments / processing instructions
            continue
        key, val = _local(child.tag), _element_to_obj(child)
        if key in obj:
            if not isinstance(obj[key], list):
                obj[key] = [obj[key]]
            obj[key].append(val)
        else:
            obj[key] = val
    text = (el.text or "").strip()
    if not obj:
        return _infer(text) if text else None
    if text:
        obj["#text"] = _infer(text)
    return obj


def read_xml(src: Path):
    from defusedxml import ElementTree as SafeET
    from defusedxml import DefusedXmlException
    try:
        root = SafeET.parse(str(src)).getroot()
    except DefusedXmlException:
        raise ConversionError(f"{src.name} uses XML entities or DTDs, which aren't allowed for safety.")
    except ET.ParseError as exc:
        raise ConversionError(f"{src.name} isn't valid XML ({exc}).")
    return {_local(root.tag): _element_to_obj(root)}


def read_xlsx(src: Path):
    from openpyxl import load_workbook
    try:
        wb = load_workbook(str(src), read_only=True, data_only=True)
    except Exception as exc:
        raise ConversionError(f"Can't open {src.name} as an Excel workbook ({exc}).")
    try:
        sheets = {ws.title: _rows_to_records([list(r) for r in ws.iter_rows(values_only=True)]) for ws in wb.worksheets}
    finally:
        wb.close()
    if len(sheets) == 1:
        return next(iter(sheets.values()))
    return sheets


READERS = {"csv": read_csv, "json": read_json, "xml": read_xml, "yaml": read_yaml, "yml": read_yaml, "xlsx": read_xlsx}


# ---------- Finding tables inside nested data ----------
def _is_table(v) -> bool:
    return isinstance(v, list)


def _tables(data, default_name: str) -> list[tuple[str, list]]:
    """Find the row list(s) inside any structure.

    [..]                       -> one table
    {"people": {"person": [..]}} -> unwraps single-key wrappers (typical XML)
    {"Sheet1": [..], "Sheet2": [..]} -> one table per key (Excel sheets)
    {...anything else}         -> a single row
    """
    name = default_name
    while isinstance(data, dict) and len(data) == 1:
        key, inner = next(iter(data.items()))
        if not isinstance(inner, (dict, list)):
            break
        name, data = key, inner
    if isinstance(data, list):
        return [(name, data)]
    if isinstance(data, dict) and data and all(_is_table(v) for v in data.values()):
        return [(str(k), v) for k, v in data.items()]
    return [(name, [data])]


def _flatten(row, prefix: str = "") -> dict:
    if not isinstance(row, dict):
        return {prefix or "value": row}
    flat = {}
    for k, v in row.items():
        key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict) and v:
            flat.update(_flatten(v, key))
        else:
            flat[key] = v
    return flat


def _grid(rows: list) -> tuple[list[str], list[list]]:
    flat = [_flatten(r) for r in rows]
    headers = []
    for r in flat:
        for k in r:
            if k not in headers:
                headers.append(k)
    return headers, [[r.get(h) for h in headers] for r in flat]


# ---------- Writers ----------
def write_json(data, out: Path) -> None:
    out.write_text(json.dumps(_plain(data), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_yaml(data, out: Path) -> None:
    import yaml
    out.write_text(yaml.safe_dump(_plain(data), sort_keys=False, allow_unicode=True), encoding="utf-8")


_XML_BAD = re.compile(r"[^\w.\-]")


def _xml_name(name) -> str:
    name = _XML_BAD.sub("_", str(name)) or "item"
    if not re.match(r"[A-Za-z_]", name) or name.lower().startswith("xml"):
        name = "_" + name
    return name


_XML_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _xml_text(value) -> str:
    return _XML_ILLEGAL.sub("", _scalar_text(value))


def _fill(el, value, item_tag: str = "item") -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            k = str(k)
            if k.startswith("@") and not isinstance(v, (dict, list)):
                el.set(_xml_name(k[1:]), _xml_text(v))
            elif k == "#text":
                el.text = _xml_text(v)
            elif isinstance(v, list):
                for item in v:
                    _fill(ET.SubElement(el, _xml_name(k)), item)
            else:
                _fill(ET.SubElement(el, _xml_name(k)), v)
    elif isinstance(value, list):
        for item in value:
            _fill(ET.SubElement(el, item_tag), item)
    elif value is not None:
        el.text = _xml_text(value)


def write_xml(data, out: Path) -> None:
    if isinstance(data, dict) and len(data) == 1 and not str(next(iter(data))).startswith(("@", "#")):
        key, inner = next(iter(data.items()))
        root = ET.Element(_xml_name(key))  # {"people": [..]} -> <people><item>..</item></people>
        _fill(root, inner)
    elif isinstance(data, list):
        root = ET.Element("rows")
        _fill(root, data, item_tag="row")
    else:
        root = ET.Element("root")
        _fill(root, data)
    ET.indent(root)
    ET.ElementTree(root).write(str(out), encoding="utf-8", xml_declaration=True)


def write_csv(data, out: Path, stem: str) -> list[Path]:
    tables = _tables(data, stem)
    outs = []
    for name, rows in tables:
        path = out if len(tables) == 1 else out.with_name(f"{stem}_{_file_part(name)}.csv")
        headers, grid = _grid(rows)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if headers:
                w.writerow(headers)
            w.writerows([[_scalar_text(v) for v in row] for row in grid])
        outs.append(path)
    return outs


def _file_part(name: str) -> str:
    return re.sub(r"[^\w\-]+", "_", str(name)).strip("_") or "table"


def _sheet_title(name: str, used: set) -> str:
    title = re.sub(r"[\[\]:*?/\\]", "_", str(name))[:31] or "Sheet"
    base, n = title, 1
    while title.lower() in used:
        n += 1
        title = f"{base[:28]}_{n}"
    used.add(title.lower())
    return title


def write_xlsx(data, out: Path, stem: str) -> None:
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

    wb = Workbook(write_only=True)
    used = set()
    for name, rows in _tables(data, stem):
        ws = wb.create_sheet(_sheet_title(name, used))
        headers, grid = _grid(rows)
        if headers:
            ws.append(headers)
        for row in grid:
            cells = []
            for v in row:
                if isinstance(v, (dict, list)):
                    v = _scalar_text(v)
                elif not (v is None or isinstance(v, (str, int, float, bool, dt.datetime, dt.date, dt.time))):
                    v = str(v)
                if isinstance(v, (dt.datetime, dt.time)) and v.tzinfo:
                    v = v.isoformat()  # Excel can't store time zones
                if isinstance(v, str):
                    v = ILLEGAL_CHARACTERS_RE.sub("", v)
                cell = WriteOnlyCell(ws, value=v)
                if isinstance(v, str) and v.startswith("="):
                    cell.data_type = "s"  # keep "=..." as text, never a live formula
                cells.append(cell)
            ws.append(cells)
    wb.save(str(out))


# ---------- Markdown ----------
DOC_CSS = """
body { font-family: sans-serif; line-height: 1.5; color: #1f2937; }
h1, h2, h3, h4 { color: #111827; }
code, pre { font-family: monospace; background-color: #f3f4f6; }
pre { padding: 8px; }
table { border-collapse: collapse; }
th, td { border: 1px solid #d1d5db; padding: 4px 8px; }
blockquote { color: #4b5563; margin-left: 12px; padding-left: 12px; border-left: 3px solid #d1d5db; }
"""


def markdown_to_html_body(src: Path) -> str:
    import markdown
    return markdown.markdown(_read_text(src), extensions=["extra", "sane_lists"], output_format="html")


def write_markdown_html(src: Path, out: Path) -> None:
    body = markdown_to_html_body(src)
    title = html.escape(src.stem)
    out.write_text(
        f'<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">\n<title>{title}</title>\n'
        f"<style>{DOC_CSS}body {{ max-width: 760px; margin: 40px auto; padding: 0 16px; }}</style>\n"
        f"</head>\n<body>\n{body}\n</body>\n</html>\n",
        encoding="utf-8",
    )


def write_markdown_pdf(src: Path, out: Path) -> None:
    import pymupdf
    body = markdown_to_html_body(src)
    body = re.sub(r"<img\b[^>]*>", "", body)  # images point at files we don't have
    story = pymupdf.Story(html=body, user_css=DOC_CSS)
    page = pymupdf.paper_rect("a4")
    frame = page + (54, 54, -54, -54)  # 0.75 inch margins
    writer = pymupdf.DocumentWriter(str(out))
    try:
        more = True
        while more:
            device = writer.begin_page(page)
            more, _ = story.place(frame)
            story.draw(device)
            writer.end_page()
    finally:
        writer.close()


# ---------- Entry point ----------
def convert_data(src: Path, target: str, out_dir: Path) -> list[Path]:
    ext = src.suffix.lower().lstrip(".")
    out = out_dir / f"{src.stem}.{target}"
    try:
        if ext == "md":
            if target == "html":
                write_markdown_html(src, out)
            elif target == "pdf":
                write_markdown_pdf(src, out)
            else:
                raise ConversionError(f"Can't convert Markdown to {target.upper()} here.")
            return [out]

        data = READERS[ext](src)
        if target == "csv":
            return write_csv(data, out, src.stem)
        if target == "xlsx":
            write_xlsx(data, out, src.stem)
        elif target == "json":
            write_json(data, out)
        elif target == "yaml":
            write_yaml(data, out)
        elif target == "xml":
            write_xml(data, out)
        else:
            raise ConversionError(f"Can't convert {ext.upper()} to {target.upper()}.")
        return [out]
    except ConversionError:
        raise
    except RecursionError:
        raise ConversionError(f"{src.name} is nested too deeply to convert.")
    except Exception as exc:
        raise ConversionError(f"Couldn't convert {src.name} to {target.upper()} ({exc}).")
