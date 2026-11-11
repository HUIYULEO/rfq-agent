import shutil
import unittest
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile
from rfq_agent.reporting.docx_preview import render
from rfq_agent.domain.terms import check


class DraftPreviewTests(unittest.TestCase):
    def test_reads_saved_document_and_escapes_text(self):
        # A plain directory, not TemporaryDirectory: on Windows that helper's ACL
        # leaves the directory unwritable, so the file inside it could never be created.
        directory = Path(__file__).parents[1] / "runtime" / f"draft-preview-{uuid4().hex}"
        directory.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, directory, True)
        path = directory / "quote-draft-r3.docx"
        with ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>&lt;script&gt;草案</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>133.04</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>')
        before = path.read_bytes()
        html = render(path)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("<table>", html)
        self.assertIn("133.04", html)
        self.assertEqual(before, path.read_bytes())

    def test_general_request_is_not_manual_but_constraints_stay_manual(self):
        rows = check({"requirements": [{"text": "请协助报价，详见附件。"}, {"text": "请协助报价，详见附件。必须提供原厂证书。"}]}, [])
        self.assertEqual(rows[0]["check"], "general_request")
        self.assertEqual(rows[0]["status"], "na")
        self.assertEqual(rows[1]["status"], "manual")