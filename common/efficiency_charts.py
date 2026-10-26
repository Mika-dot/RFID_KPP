"""Small native OOXML charts for the standard-library production exporter."""
from xml.sax.saxutils import escape

C = 'http://schemas.openxmlformats.org/drawingml/2006/chart'
A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
D = 'http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'


def chart_xml(sheet_name, chart, values):
    first, last, category, columns = chart['first'], chart['last'], chart['category'], chart['columns']
    colors = ['287CA5', 'CC8D34', '638C72', '9A607F']
    quoted = "'"+sheet_name.replace("'", "''")+"'!"
    from common.efficiency_xlsx import col
    def title(text):
        return ('<c:title><c:tx><c:rich><a:bodyPr/><a:lstStyle/><a:p><a:r><a:rPr lang="ru-RU" sz="1200"/>'
                '<a:t>'+escape(text)+'</a:t></a:r></a:p></c:rich></c:tx><c:overlay val="0"/></c:title>')
    def formula(column):
        return escape(f'{quoted}${col(column)}${first}:${col(column)}${last}')
    categories = [str(values[row-1][category-1]) for row in range(first, last+1)]
    maximum = max((values[row-1][column-1] for row in range(first,last+1) for column in columns
                   if isinstance(values[row-1][column-1], (int,float))),default=0)
    series = []
    for index, column in enumerate(columns):
        name = values[first-2][column-1]
        points = [values[row-1][column-1] for row in range(first, last+1)]
        series.append(f'<c:ser><c:idx val="{index}"/><c:order val="{index}"/>'
                      '<c:tx><c:v>'+escape(str(name))+'</c:v></c:tx>'
                      f'<c:spPr><a:solidFill><a:srgbClr val="{colors[index % len(colors)]}"/></a:solidFill>'
                      f'<a:ln w="19050"><a:solidFill><a:srgbClr val="{colors[index % len(colors)]}"/></a:solidFill></a:ln></c:spPr>'
                      '<c:cat><c:strRef><c:f>'+formula(category)+'</c:f><c:strCache>'
                      f'<c:ptCount val="{len(categories)}"/>'
                      +''.join(f'<c:pt idx="{i}"><c:v>{escape(v)}</c:v></c:pt>' for i, v in enumerate(categories))
                      +'</c:strCache></c:strRef></c:cat><c:val><c:numRef><c:f>'+formula(column)+'</c:f><c:numCache>'
                      '<c:formatCode>0</c:formatCode>'+f'<c:ptCount val="{len(points)}"/>'
                      +''.join(f'<c:pt idx="{i}"><c:v>{v}</c:v></c:pt>' for i, v in enumerate(points) if isinstance(v, (int, float)))
                      +'</c:numCache></c:numRef></c:val></c:ser>')
    if chart.get('kind') == 'line':
        plot = '<c:lineChart><c:grouping val="standard"/>'+''.join(series)+'<c:marker val="0"/><c:smooth val="0"/><c:axId val="1"/><c:axId val="2"/></c:lineChart>'
    else:
        plot = '<c:barChart><c:barDir val="col"/><c:grouping val="clustered"/>'+''.join(series)+'<c:gapWidth val="80"/><c:axId val="1"/><c:axId val="2"/></c:barChart>'
    return (f'<?xml version="1.0" encoding="UTF-8"?><c:chartSpace xmlns:c="{C}" xmlns:a="{A}">'
            '<c:lang val="ru-RU"/><c:chart>'+title(chart['title'])+'<c:autoTitleDeleted val="0"/><c:plotArea><c:layout/>'+plot
            +'<c:catAx><c:axId val="1"/><c:scaling><c:orientation val="minMax"/></c:scaling><c:axPos val="b"/>'
            '<c:tickLblPos val="nextTo"/><c:crossAx val="2"/><c:crosses val="autoZero"/><c:auto val="1"/><c:lblAlgn val="ctr"/></c:catAx>'
            '<c:valAx><c:axId val="2"/><c:scaling><c:orientation val="minMax"/><c:min val="0"/>'
            + ('<c:max val="1"/>' if maximum==0 else '')+'</c:scaling><c:axPos val="l"/>'
            '<c:majorGridlines/><c:numFmt formatCode="0" sourceLinked="0"/><c:tickLblPos val="nextTo"/>'
            '<c:crossAx val="1"/><c:crosses val="autoZero"/><c:crossBetween val="between"/>'
            + ('<c:majorUnit val="1"/>' if maximum<=5 else '')+'</c:valAx></c:plotArea>'
            '<c:legend><c:legendPos val="b"/><c:overlay val="0"/></c:legend><c:plotVisOnly val="1"/>'
            '<c:dispBlanksAs val="gap"/></c:chart><c:txPr><a:bodyPr/><a:lstStyle/><a:p>'
            '<a:pPr><a:defRPr sz="1000"><a:latin typeface="Carlito"/></a:defRPr></a:pPr>'
            '<a:endParaRPr lang="ru-RU"/></a:p></c:txPr></c:chartSpace>')


def drawing_xml(charts):
    parts = []
    for index, chart in enumerate(charts, 1):
        row = chart['row']-1
        parts.append(f'<xdr:oneCellAnchor><xdr:from><xdr:col>0</xdr:col><xdr:colOff>0</xdr:colOff>'
                     f'<xdr:row>{row}</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>'
                     '<xdr:ext cx="9906000" cy="2476500"/>'
                     '<xdr:graphicFrame macro=""><xdr:nvGraphicFramePr>'
                     f'<xdr:cNvPr id="{index}" name="Chart {index}"/><xdr:cNvGraphicFramePr/></xdr:nvGraphicFramePr>'
                     '<xdr:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/></xdr:xfrm>'
                     '<a:graphic><a:graphicData uri="'+C+'">'
                     f'<c:chart xmlns:c="{C}" xmlns:r="{R}" r:id="rId{index}"/>'
                     '</a:graphicData></a:graphic></xdr:graphicFrame><xdr:clientData/></xdr:oneCellAnchor>')
    return f'<?xml version="1.0" encoding="UTF-8"?><xdr:wsDr xmlns:xdr="{D}" xmlns:a="{A}">'+''.join(parts)+'</xdr:wsDr>'
