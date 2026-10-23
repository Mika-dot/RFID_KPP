"""Dependency-free OOXML export for the production Python Web service.

Uses the same ZIP/XML approach as the existing passage exporter. No Office,
Node, external service or additional deployment dependency is required.
"""
from __future__ import annotations

import io
import math
import zipfile
from datetime import date
from xml.sax.saxutils import escape

from common.efficiency_report import (HEADERS, effectiveness_rows, email_parts, exceptions_text,
                                      explanatory_text, methodology_text, period_text, summary_rows)

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"


def clean(value):
    return ''.join(c for c in str(value) if c in '\n\t\r' or ord(c) >= 32)


def col(index):
    out = ""
    while index:
        index, digit = divmod(index-1, 26)
        out = chr(65+digit) + out
    return out


class Sheet:
    def __init__(self, widths):
        self.widths, self.rows, self.merges = widths, [], []

    def row(self, values=(), style=0, height=24):
        number = len(self.rows)+1
        cells = []
        for index, value in enumerate(values, 1):
            cell_style = style
            if isinstance(value, tuple):
                value, cell_style = value
            address = f'{col(index)}{number}'
            if isinstance(value, dict):
                formula, cached = value['formula'], value.get('value')
                # All formulas here are trusted application expressions.
                cells.append(f'<c r="{address}" s="{cell_style}"'+(' t="str"' if cached == '' else '')
                             +f'><f>{escape(formula)}</f><v>{escape(str(cached))}</v></c>')
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                cells.append(f'<c r="{address}" s="{cell_style}"><v>{value}</v></c>')
            else:
                # inlineStr deliberately prevents spreadsheet formula injection.
                cells.append(f'<c r="{address}" s="{cell_style}" t="inlineStr"><is><t xml:space="preserve">'
                             +escape(clean(value))+ '</t></is></c>')
        self.rows.append(f'<row r="{number}" ht="{height}" customHeight="1">'+''.join(cells)+'</row>')
        return number

    def text(self, text, style=0):
        # Split paragraphs instead of hiding long text beyond Excel's row limit.
        for paragraph in text.split('\n'):
            while len(paragraph) > 850:
                split = paragraph.rfind(' ', 0, 850)
                self.text(paragraph[:split], style)
                paragraph = paragraph[split+1:]
            number = self.row([paragraph], style, max(25, 16*math.ceil(len(paragraph)/115)+8))
            self.merges.append(f'A{number}:{col(len(self.widths))}{number}')

    def xml(self, frozen=False):
        columns = ''.join(f'<col min="{i}" max="{i}" width="{width}" customWidth="1"/>' for i, width in enumerate(self.widths, 1))
        merges = ('<mergeCells count="'+str(len(self.merges))+'">'+''.join(f'<mergeCell ref="{m}"/>' for m in self.merges)+'</mergeCells>') if self.merges else ''
        pane = '<pane ySplit="4" topLeftCell="A5" activePane="bottomLeft" state="frozen"/>' if frozen else ''
        return (f'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="{NS}">'
                '<sheetPr><pageSetUpPr fitToPage="1"/></sheetPr>'
                f'<dimension ref="A1:{col(len(self.widths))}{len(self.rows)}"/>'
                f'<sheetViews><sheetView workbookViewId="0" showGridLines="0">{pane}</sheetView></sheetViews>'
                '<sheetFormatPr defaultRowHeight="24"/>'+f'<cols>{columns}</cols><sheetData>'+''.join(self.rows)+'</sheetData>'+merges
                +'<pageMargins left="0.3" right="0.3" top="0.4" bottom="0.4" header="0.2" footer="0.2"/>'
                '<pageSetup paperSize="9" orientation="landscape" fitToWidth="1" fitToHeight="0"/></worksheet>')


def styles_xml():
    def xf(font=0, fill=0, number=0, alignment='left'):
        return (f'<xf numFmtId="{number}" fontId="{font}" fillId="{fill}" borderId="0" xfId="0" applyAlignment="1"'
                + (' applyNumberFormat="1"' if number else '')+f'><alignment horizontal="{alignment}" vertical="center" wrapText="1"/></xf>')
    return (f'<?xml version="1.0" encoding="UTF-8"?><styleSheet xmlns="{NS}">'
            '<numFmts count="1"><numFmt numFmtId="164" formatCode="dd.mm"/></numFmts>'
            '<fonts count="3"><font><sz val="11"/><color rgb="FF16364D"/><name val="Carlito"/></font>'
            '<font><b/><sz val="15"/><color rgb="FFFFFFFF"/><name val="Carlito"/></font>'
            '<font><b/><sz val="11"/><color rgb="FF16364D"/><name val="Carlito"/></font></fonts>'
            '<fills count="4"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
            '<fill><patternFill patternType="solid"><fgColor rgb="FF16364D"/><bgColor indexed="64"/></patternFill></fill>'
            '<fill><patternFill patternType="solid"><fgColor rgb="FFDFE8EF"/><bgColor indexed="64"/></patternFill></fill></fills>'
            '<borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            '<cellXfs count="9">'+xf()+xf(1, 2)+xf(2, 3)+xf(number=10, alignment='center')+xf(2, 3, 10, 'center')+xf(2)+xf(alignment='center')+xf(number=164, alignment='center')+xf(2, 3, alignment='center')+'</cellXfs>'
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')


def generate_efficiency_xlsx(report, include_details=False):
    sheet = Sheet([20, 23, 18, 22, 18, 32])
    sheet.text('ЭФФЕКТИВНОСТЬ RFID КПП «ПЕРИМЕТР»', 1)
    sheet.text('Период: '+period_text(report)+' • МСК')
    sheet.row()
    sheet.row(HEADERS, 2, 36)
    for day, display in zip(report['days'], summary_rows(report)):
        n = len(sheet.rows)+1
        dt = date.fromisoformat(day['date'])
        serial = (dt-date(1899, 12, 30)).days-int(dt < date(1900, 3, 1))
        sheet.row([(serial, 7), *[(value, 6) for value in display[1:4]], ({'formula': f'IF(C{n}=0,"",D{n}/C{n})',
                                  'value': day['detected']/day['tagged'] if day['tagged'] else ''}, 3), display[5]])
    n, last = len(sheet.rows)+1, len(sheet.rows)
    t = report['totals']
    sheet.row(['Итого', ({'formula': f'SUM(B5:B{last})', 'value': t['total']}, 8),
               ({'formula': f'SUM(C5:C{last})', 'value': t['tagged']}, 8),
               ({'formula': f'SUM(D5:D{last})', 'value': t['detected']}, 8),
               ({'formula': f'IF(C{n}=0,"",D{n}/C{n})', 'value': t['detected']/t['tagged'] if t['tagged'] else ''}, 4),
               summary_rows(report)[-1][-1]], 2)
    sheet.row()
    sheet.text(explanatory_text(report))
    sheet.row()
    sheet.row(['Показатель', '', 'Склад / 1С', 'Периметр', '', 'Эффективность'], 2, 34)
    header = len(sheet.rows)
    sheet.merges.extend([f'A{header}:B{header}', f'D{header}:E{header}'])
    for values in effectiveness_rows(report):
        row = sheet.row([values[0], '', values[1], values[2], '', values[3]], height=38)
        sheet.merges.extend([f'A{row}:B{row}', f'D{row}:E{row}'])
    sheet.text(exceptions_text(report))
    if any(row['ambiguous_tags'] for row in report['details']):
        sheet.text('Обнаружены метки, связанные с несколькими сериями. Они не засчитываются как подтверждённое RFID-считывание.')
    sheet.row()
    sheet.text('ТЕКСТ ПИСЬМА', 1)
    email = email_parts(report)
    sheet.text('Тема: '+email['subject'], 5)
    for i, block in enumerate(email['blocks']):
        if i == 2:
            sheet.row(HEADERS, 2, 36)
            for values in summary_rows(report):
                sheet.row(values)
        elif i == 6:
            for values in [['Показатель', 'Склад / 1С', 'Периметр', 'Эффективность'], *effectiveness_rows(report)]:
                row = sheet.row([values[0], '', values[1], values[2], '', values[3]], height=38)
                sheet.merges.extend([f'A{row}:B{row}', f'D{row}:E{row}'])
        else:
            sheet.text(block)
        sheet.row()
    sheet.text('Методика v'+report['version']+' • Сформировано: '+report['generated_at'])
    sheet.text(methodology_text(report))
    sheet.text(f'Исключено записей МК/MK: {report["excluded_mk"]}; повторных записей серий: {report["duplicate_warehouse_rows"]}.')
    sheets = [('Отчёт', sheet)]
    if include_details:
        detail = Sheet([17, 23, 22, 14, 14, 16, 60, 60, 25, 45])
        detail.row(['Серия', 'Дата склада (МСК)', 'ID склада', 'С RFID', 'Считано', 'Периметр',
                    'Полные метки 1С', 'Совпавшие полные метки', 'ID событий КПП', 'Неоднозначные метки'], 2, 40)
        for row in report['details']:
            tags = max(len(row['registered_tags']), len(row['matched_tags']), 1)
            detail.row([row['series'], row['warehouse_dt'], '; '.join(row['warehouse_ids']), int(row['has_rfid']),
                        int(row['detected']), int(row['in_perimeter']), '\n'.join(row['registered_tags']),
                        '\n'.join(row['matched_tags']), '; '.join(row['event_ids']), '\n'.join(row['ambiguous_tags'])],
                       height=min(400, max(30, 30*tags)))
        sheets.append(('Сверка', detail))
    content_types = (f'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                     '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                     '<Default Extension="xml" ContentType="application/xml"/>'
                     '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                     '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                     +''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1, len(sheets)+1))+'</Types>')
    workbook = (f'<workbook xmlns="{NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                +''.join(f'<sheet name="{name}" sheetId="{i}" r:id="rId{i}"/>' for i, (name, _) in enumerate(sheets, 1))
                +'</sheets><calcPr calcId="191029" fullCalcOnLoad="1"/></workbook>')
    relationships = (f'<Relationships xmlns="{REL}">'
                     +''.join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1, len(sheets)+1))
                     +f'<Relationship Id="rId{len(sheets)+1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', content_types)
        archive.writestr('_rels/.rels', f'<Relationships xmlns="{REL}"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        archive.writestr('xl/workbook.xml', workbook)
        archive.writestr('xl/_rels/workbook.xml.rels', relationships)
        archive.writestr('xl/styles.xml', styles_xml())
        for i, (_, item) in enumerate(sheets, 1):
            archive.writestr(f'xl/worksheets/sheet{i}.xml', item.xml(frozen=i == 1))
    return out.getvalue()
