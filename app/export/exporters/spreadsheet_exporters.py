"""Spreadsheet exporters (XLSX via openpyxl).

Registered only when ``openpyxl`` is importable at runtime so deployments without
it keep every other exporter working.
"""

from __future__ import annotations

import io

from app.export.registry import Exporter, ExportPayload


class XlsxExporter(Exporter):
    format_name = "xlsx"

    def render(self, payload: ExportPayload) -> bytes:
        from openpyxl import Workbook
        from openpyxl.styles import Font

        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = "Export"
        if payload.headers:
            worksheet.append(list(payload.headers))
            for cell in worksheet[1]:
                cell.font = Font(bold=True)
        if payload.rows:
            for row in payload.rows:
                worksheet.append([str(cell) for cell in row])
        if not payload.rows and payload.content:
            tokens = [line.split("\t") for line in payload.content.splitlines() if line.strip()]
            for row in tokens:
                worksheet.append(row)
        stream = io.BytesIO()
        workbook.save(stream)
        return stream.getvalue()
