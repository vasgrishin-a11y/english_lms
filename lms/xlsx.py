"""Минимальный XLSX-писатель: журнал выгружается книгой Excel, а не CSV.

XLSX — это zip с XML, поэтому модуль опирается только на стандартные ``zipfile``
и ``xml.sax.saxutils``. Никаких новых зависимостей: ``requirements.txt`` с
хешами остаётся прежним, а учитель получает файл, который Excel, LibreOffice и
Google Таблицы открывают без «мастера импорта» и без потери кириллицы.
"""

from __future__ import annotations

import re
import zipfile
from io import BytesIO

CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_WORKBOOK_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

# 0 — обычная ячейка, 1 — заголовок (жирный, перенос, серый фон),
# 2 — число, 3 — широкая текстовая ячейка с переносом.
_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2">
<font><sz val="11"/><name val="Calibri"/></font>
<font><b/><sz val="11"/><name val="Calibri"/></font>
</fonts>
<fills count="3">
<fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFEDF2EE"/><bgColor indexed="64"/></patternFill></fill>
</fills>
<borders count="2">
<border><left/><right/><top/><bottom/><diagonal/></border>
<border><left style="thin"><color rgb="FFD6DAD4"/></left><right style="thin"><color rgb="FFD6DAD4"/></right><top style="thin"><color rgb="FFD6DAD4"/></top><bottom style="thin"><color rgb="FFD6DAD4"/></bottom></border>
</borders>
<cellXfs count="4">
<xf numFmtId="0" fontId="0" fillId="0" borderId="1" applyBorder="1"><alignment vertical="top"/></xf>
<xf numFmtId="0" fontId="1" fillId="2" borderId="1" applyFont="1" applyFill="1" applyBorder="1"><alignment vertical="center" wrapText="1"/></xf>
<xf numFmtId="0" fontId="0" fillId="0" borderId="1" applyBorder="1"><alignment horizontal="right" vertical="top"/></xf>
<xf numFmtId="0" fontId="0" fillId="0" borderId="1" applyBorder="1"><alignment vertical="top" wrapText="1"/></xf>
</cellXfs>
</styleSheet>"""

_INVALID_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_ESCAPES = (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"))


def escape(value):
    """Экранировать спецсимволы XML. Этого достаточно: атрибуты — не наши данные.

    ``escape`` из ``xml.sax.saxutils`` дал бы то же самое, но bandit помечает его
    как B406 («escape для недоверенного XML»): здесь подстановок в XML-структуру
    нет, поэтому обходимся парой замен — без подавления предупреждений.
    """
    text = str(value if value is not None else "")
    for symbol, entity in _ESCAPES:
        text = text.replace(symbol, entity)
    return text


def _text(value):
    """Значение → безопасный текст ячейки (кириллица не экранируется дважды)."""
    return _INVALID_XML.sub("", escape(value))


def _column(index):
    """0 → A, 25 → Z, 26 → AA."""
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _numeric(value):
    """Число, если ячейка действительно число: проценты и баллы удобно считать."""
    text = str(value).strip()
    if not text or len(text) > 15:
        return None
    if re.fullmatch(r"-?\d+(?:[.,]\d+)?", text):
        return text.replace(",", ".")
    return None


def _cell(reference, value, style):
    number = _numeric(value) if style != 1 else None
    if number is not None and style != 3:
        return f'<c r="{reference}" s="2"><v>{number}</v></c>'
    return (
        f'<c r="{reference}" s="{style}" t="inlineStr">'
        f'<is><t xml:space="preserve">{_text(value)}</t></is></c>'
    )


def _column_widths(rows):
    """Ширина по самому длинному значению: узкие колонки не режут заголовки."""
    widths = []
    for row in rows:
        for index, value in enumerate(row):
            length = min(60, max(len(line) for line in str(value or "").splitlines() or [""]) + 2)
            if index >= len(widths):
                widths.append(length)
            else:
                widths[index] = max(widths[index], length)
    return [min(52, max(10, width)) for width in widths]


def build_xlsx(rows, *, sheet_name="Лист1"):
    """Собрать XLSX из списка строк (первая строка — заголовок).

    Возвращает ``bytes`` готовой книги: одна страница, закреплённая шапка,
    автофильтр, перенос текста в заголовках и разумная ширина колонок.
    """
    table = [[("" if cell is None else cell) for cell in row] for row in rows]
    if not table:
        table = [[""]]
    width = max(len(row) for row in table)
    table = [row + [""] * (width - len(row)) for row in table]
    header = f'<autoFilter ref="A1:{_column(width - 1)}{len(table)}"/>' if len(table) > 1 else ""
    body = []
    for row_index, row in enumerate(table, start=1):
        style = 1 if row_index == 1 else 0
        cells = "".join(
            _cell(f"{_column(index)}{row_index}", value, style) for index, value in enumerate(row)
        )
        body.append(f'<row r="{row_index}">{cells}</row>')
    columns = "".join(
        f'<col min="{index + 1}" max="{index + 1}" width="{value}" customWidth="1"/>'
        for index, value in enumerate(_column_widths(table))
    )
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetViews><sheetView workbookViewId="0">'
        f'<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        f"</sheetView></sheetViews>"
        '<sheetFormatPr defaultRowHeight="15"/>'
        f"<cols>{columns}</cols>"
        f"<sheetData>{''.join(body)}</sheetData>"
        f"{header}"
        "</worksheet>"
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{_text(sheet_name)[:31]}" sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>"
    )
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _ROOT_RELS)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS)
        archive.writestr("xl/styles.xml", _STYLES)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    return buffer.getvalue()


__all__ = ["CONTENT_TYPE", "build_xlsx", "escape"]
