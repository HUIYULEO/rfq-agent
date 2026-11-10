"""HTML content preview of the saved DOCX, without external conversion services."""
from html import escape
from zipfile import ZipFile
from xml.etree import ElementTree as ET
W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'

def render(path):
    with ZipFile(path) as archive:
        body = ET.fromstring(archive.read('word/document.xml')).find(W+'body')
    def block(node):
        if node.tag == W+'p':
            return '<p>'+''.join(escape(n.text or '') if n.tag == W+'t' else '<br>' for n in node.iter() if n.tag in {W+'t',W+'br'})+'</p>'
        if node.tag == W+'tbl':
            rows=[]
            for row in node.findall(W+'tr'):
                cells=[]
                for cell in row.findall(W+'tc'):
                    span=cell.find(W+'tcPr/'+W+'gridSpan')
                    count=int(span.get(W+'val','1')) if span is not None else 1
                    cells.append(f'<td colspan="{count}">'+''.join(block(n) for n in cell)+'</td>')
                rows.append('<tr>'+''.join(cells)+'</tr>')
            return '<div class="table-scroll"><table>'+''.join(rows)+'</table></div>'
        return ''
    return ''.join(block(n) for n in body)
