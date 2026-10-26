"""Dependency-free OOXML export for the production Python Web service.

Uses the same ZIP/XML approach as the existing passage exporter. No Office,
Node, external service or additional deployment dependency is required.
"""
from __future__ import annotations

import io
import math
import zipfile
from datetime import datetime
from xml.sax.saxutils import escape

from common.efficiency_diagnostics import reconcile
from common.efficiency_cases import attach_case_analysis
from common.efficiency_charts import chart_xml, drawing_xml

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
        self.widths, self.rows, self.merges, self.values = widths, [], [], []
        self.links, self.charts = [], []
        self.freeze = 0
        self.filter = None
        self.one_page = False
        self.validations = ''

    def row(self, values=(), style=0, height=24):
        number = len(self.rows)+1
        cells = []
        raw_values = []
        for index, value in enumerate(values, 1):
            cell_style = style
            if isinstance(value, tuple):
                value, cell_style = value
            if value is None:
                value = ''
            address = f'{col(index)}{number}'
            if isinstance(value, dict):
                if 'link' in value:
                    self.links.append((address, value['link']))
                    value = value['text']
            raw_values.append(value.get('value') if isinstance(value, dict) else value)
            if isinstance(value, datetime):
                value = (value-datetime(1899, 12, 30)).total_seconds()/86400
                cell_style = 9
            if isinstance(value, dict):
                formula, cached = value['formula'], value.get('value')
                # All formulas here are trusted application expressions.
                cells.append(f'<c r="{address}" s="{cell_style}"'+(' t="str"' if cached == '' else '')
                             +f'><f>{escape(formula)}</f><v>{escape(str(cached))}</v></c>')
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                cells.append(f'<c r="{address}" s="{cell_style}"><v>{value}</v></c>')
            else:
                if len(clean(value or '')) > 32767:
                    raise ValueError('Слишком много доказательств в одной ячейке Excel; выберите меньший период.')
                # inlineStr deliberately prevents spreadsheet formula injection.
                cells.append(f'<c r="{address}" s="{cell_style}" t="inlineStr"><is><t xml:space="preserve">'
                             +escape(clean(value))+ '</t></is></c>')
        self.rows.append(f'<row r="{number}" ht="{height}" customHeight="1">'+''.join(cells)+'</row>')
        self.values.append(raw_values)
        return number

    def text(self, text, style=0):
        # Split paragraphs instead of hiding long text beyond Excel's row limit.
        for paragraph in text.split('\n'):
            while len(paragraph) > 850:
                split = paragraph.rfind(' ', 0, 850)
                if split <= 0:
                    split = 850
                self.text(paragraph[:split], style)
                paragraph = paragraph[split+1:]
            number = self.row([paragraph], style, max(25, 16*math.ceil(len(paragraph)/115)+8))
            self.merges.append(f'A{number}:{col(len(self.widths))}{number}')

    def pairs(self, values, style=0, height=36):
        """Four readable two-column fields in an eight-column case sheet."""
        number = self.row([v for value in values for v in (value, '')], style, height)
        self.merges.extend(f'{col(c)}{number}:{col(c+1)}{number}' for c in range(1, 9, 2))
        return number

    def chart(self, title, first, last, columns, row, *, category=1, kind='bar'):
        if first <= last:
            self.charts.append(dict(title=title,first=first,last=last,columns=columns,row=row,category=category,kind=kind))

    def reserve_chart(self):
        first = len(self.rows)+1
        for _ in range(12):
            self.row(height=18)
        return first

    def xml(self):
        columns = ''.join(f'<col min="{i}" max="{i}" width="{width}" customWidth="1"/>' for i, width in enumerate(self.widths, 1))
        merges = ('<mergeCells count="'+str(len(self.merges))+'">'+''.join(f'<mergeCell ref="{m}"/>' for m in self.merges)+'</mergeCells>') if self.merges else ''
        pane = f'<pane ySplit="{self.freeze}" topLeftCell="A{self.freeze+1}" activePane="bottomLeft" state="frozen"/>' if self.freeze else ''
        hyperlinks = ('<hyperlinks>'+''.join('<hyperlink ref="'+ref+'" location="'+escape(location, {'"':'&quot;'})+'"/>' for ref,location in self.links)+'</hyperlinks>') if self.links else ''
        return (f'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="{NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheetPr><pageSetUpPr fitToPage="1"/></sheetPr>'
                f'<dimension ref="A1:{col(len(self.widths))}{len(self.rows)}"/>'
                f'<sheetViews><sheetView workbookViewId="0" showGridLines="0">{pane}</sheetView></sheetViews>'
                '<sheetFormatPr defaultRowHeight="24"/>'+f'<cols>{columns}</cols><sheetData>'+''.join(self.rows)+'</sheetData>'
                + (f'<autoFilter ref="{self.filter}"/>' if self.filter else '')+merges
                +self.validations+hyperlinks
                +'<pageMargins left="0.3" right="0.3" top="0.4" bottom="0.4" header="0.2" footer="0.2"/>'
                +f'<pageSetup paperSize="9" orientation="landscape" fitToWidth="1" fitToHeight="{1 if self.one_page else 0}"/>'
                + ('<drawing r:id="rIdDrawing"/>' if self.charts else '')+'</worksheet>')


def styles_xml():
    def xf(font=0, fill=0, number=0, alignment='left'):
        return (f'<xf numFmtId="{number}" fontId="{font}" fillId="{fill}" borderId="0" xfId="0" applyAlignment="1"'
                + (' applyNumberFormat="1"' if number else '')+f'><alignment horizontal="{alignment}" vertical="center" wrapText="1"/></xf>')
    return (f'<?xml version="1.0" encoding="UTF-8"?><styleSheet xmlns="{NS}">'
            '<numFmts count="2"><numFmt numFmtId="164" formatCode="dd.mm"/><numFmt numFmtId="165" formatCode="dd.mm.yyyy hh:mm:ss"/></numFmts>'
            '<fonts count="3"><font><sz val="11"/><color rgb="FF16364D"/><name val="Carlito"/></font>'
            '<font><b/><sz val="15"/><color rgb="FFFFFFFF"/><name val="Carlito"/></font>'
            '<font><b/><sz val="11"/><color rgb="FF16364D"/><name val="Carlito"/></font></fonts>'
            '<fills count="5"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
            '<fill><patternFill patternType="solid"><fgColor rgb="FF16364D"/><bgColor indexed="64"/></patternFill></fill>'
            '<fill><patternFill patternType="solid"><fgColor rgb="FFDFE8EF"/><bgColor indexed="64"/></patternFill></fill>'
            '<fill><patternFill patternType="solid"><fgColor rgb="FFFFF0CB"/><bgColor indexed="64"/></patternFill></fill></fills>'
            '<borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            '<cellXfs count="11">'+xf()+xf(1, 2)+xf(2, 3)+xf(number=10, alignment='center')+xf(2, 3, 10, 'center')+xf(2)+xf(alignment='center')+xf(number=164, alignment='center')+xf(2, 3, alignment='center')+xf(number=165)+xf(fill=4)+'</cellXfs>'
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')


def generate_efficiency_xlsx(report, include_details=False):
    if not report.get('raw_checked'):
        raise ValueError('Проверка исходного журнала RFID Reader не завершена; окончательный отчёт не сформирован.')
    if 'reconciliation' not in report:
        report = reconcile(report)
    if 'case_sources' not in report:
        report = attach_case_analysis(report)
    from common.efficiency_layout import report_sheets
    sheets = report_sheets(report, Sheet, include_details)
    overrides = [('workbook.xml', 'sheet.main'), ('styles.xml', 'styles')]
    overrides += [(f'worksheets/sheet{i}.xml', 'worksheet') for i in range(1,len(sheets)+1)]
    chart_count = 0
    extra = {}
    for i,(name,sheet) in enumerate(sheets,1):
        if not sheet.charts:
            continue
        overrides.append((f'drawings/drawing{i}.xml', 'drawing'))
        extra[f'xl/drawings/drawing{i}.xml'] = drawing_xml(sheet.charts)
        extra[f'xl/worksheets/_rels/sheet{i}.xml.rels'] = (
            f'<Relationships xmlns="{REL}"><Relationship Id="rIdDrawing" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing" '
            f'Target="../drawings/drawing{i}.xml"/></Relationships>')
        relationships = []
        for j,chart in enumerate(sheet.charts,1):
            chart_count += 1
            overrides.append((f'charts/chart{chart_count}.xml', 'chart'))
            extra[f'xl/charts/chart{chart_count}.xml'] = chart_xml(name,chart,sheet.values)
            relationships.append(f'<Relationship Id="rId{j}" '
                                 'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart" '
                                 f'Target="../charts/chart{chart_count}.xml"/>')
        extra[f'xl/drawings/_rels/drawing{i}.xml.rels'] = f'<Relationships xmlns="{REL}">'+''.join(relationships)+'</Relationships>'
    types = dict(drawing='application/vnd.openxmlformats-officedocument.drawing+xml',
                 chart='application/vnd.openxmlformats-officedocument.drawingml.chart+xml')
    prefix = 'application/vnd.openxmlformats-officedocument.spreadsheetml.'
    content_types = ('<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                     '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                     '<Default Extension="xml" ContentType="application/xml"/>'
                     + ''.join(f'<Override PartName="/xl/{path}" ContentType="{types.get(kind,prefix+kind+"+xml")}"/>'
                               for path,kind in overrides)+'</Types>')
    workbook = (f'<workbook xmlns="{NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                +''.join('<sheet name="'+escape(name, {'"':'&quot;'})+f'" sheetId="{i}" r:id="rId{i}"/>'
                         for i,(name,_) in enumerate(sheets,1))+'</sheets><definedNames>'
                +''.join(f'<definedName name="_xlnm.Print_Area" localSheetId="{i}">'
                         +escape("'"+name.replace("'","''")+f"'!$A$1:${col(len(sheet.widths))}${len(sheet.rows)}")
                         +'</definedName>' for i,(name,sheet) in enumerate(sheets))
                +'</definedNames><calcPr calcId="191029" fullCalcOnLoad="1"/></workbook>')
    relationships = (f'<Relationships xmlns="{REL}">'
                     +''.join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                              f'Target="worksheets/sheet{i}.xml"/>' for i in range(1,len(sheets)+1))
                     +f'<Relationship Id="rId{len(sheets)+1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml',content_types)
        archive.writestr('_rels/.rels',f'<Relationships xmlns="{REL}"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        archive.writestr('xl/workbook.xml',workbook)
        archive.writestr('xl/_rels/workbook.xml.rels',relationships)
        archive.writestr('xl/styles.xml',styles_xml())
        for i,(_,sheet) in enumerate(sheets,1):
            archive.writestr(f'xl/worksheets/sheet{i}.xml',sheet.xml())
        for path,content in extra.items():
            archive.writestr(path,content)
    return out.getvalue()
