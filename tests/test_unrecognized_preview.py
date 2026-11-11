"""An inquiry the code cannot parse must not die at the preview door.

The two "hard" fixtures are hand-written customer mails whose layout the
deterministic parser rejects on purpose: an attachment whose table is empty, and a
body that is prose only. They stand in for the judge who drags in their own mail —
the case that used to end at HTTP 400 with no way forward.

Guarantees pinned here:
  * a readable mail in an unfamiliar format is previewed, not rejected;
  * a file with nothing in it is still refused, with a clean reason;
  * the model-extraction path that already existed is actually reachable from an
    upload, and every extracted value still has to be findable in the source.

Every model call is stubbed. Note for anyone running these fixtures against a real
model: mock1_hard's body says it exists to prove a test point, so a real model
reasonably answers document_type=other and the fail-closed branch refuses it. Swap
the body for an ordinary customer sentence and the same attachment extracts fine.
"""
import shutil
import unittest
from pathlib import Path
from unittest import mock

from rfq_agent.application import cases
from rfq_agent.llm import perception
from rfq_agent.llm.genesis import GenesisError
from rfq_agent import web as web_module

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
# Scratch roots live under runtime/: TemporaryDirectory's ACL blocks child dirs here.
WORK = ROOT / "runtime" / "test_unrecognized_preview"
HARD = FIXTURES / "mock1_hard_preview_blocked.eml"
BODY_ONLY = FIXTURES / "mock2_hard_body_only_preview_blocked.eml"

CONFIG = {"GENESIS_BASE_URL": "https://x/v1", "GENESIS_MODEL": "stub",
          "GENESIS_ACCESS_TOKEN": "t"}


def _field(value, confidence=0.99, block="a1-2", row=3):
    return {"value": value, "raw": value, "confidence": confidence,
            "anchor": {"block": block, "row": row}, "evidence": []}


def _line(part, quantity, unit="EA"):
    return {"part_number": _field(part), "quantity": _field(quantity),
            "unit": _field(unit)}


class StagePreviewTest(unittest.TestCase):
    """stage_pending: an unrecognised *format* is kept, an unreadable *file* is not."""

    def setUp(self):
        self.root = WORK / self._testMethodName
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True, exist_ok=True)
        cases._CACHE.clear()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        cases._CACHE.clear()

    def test_readable_but_unrecognised_mail_is_staged_for_the_model(self):
        token, staged, rfq = cases.stage_pending(self.root, "buyer.eml", HARD.read_bytes())
        self.assertIsNone(rfq, "解析器认不出来时不该谎称读到了")
        self.assertTrue(staged.exists(), "可读的邮件必须留给模型抽取")
        self.assertTrue((self.root / cases.PENDING / f"{token}.name").exists())

    def test_prose_only_mail_without_any_table_is_staged_too(self):
        _, staged, rfq = cases.stage_pending(self.root, "body.eml", BODY_ONLY.read_bytes())
        self.assertIsNone(rfq)
        self.assertTrue(staged.exists())

    def test_file_with_nothing_in_it_is_still_refused(self):
        with self.assertRaises(ValueError):
            cases.stage_pending(self.root, "empty.eml", b"")
        self.assertEqual(list((self.root / cases.PENDING).glob("*.eml")), [],
                         "不可读的文件不该在暂存区留下垃圾")

    def test_discover_lists_the_unrecognised_mail_instead_of_hiding_it(self):
        token, _, _ = cases.stage_pending(self.root, "buyer.eml", HARD.read_bytes())
        cases.confirm_pending(self.root, token)
        listed = cases.discover(self.root, refresh=True)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].container, "unrecognized")
        self.assertEqual(listed[0].lines, 0)
        self.assertIn("未识别格式", listed[0].label)

    def test_unrecognised_mail_keeps_its_sender_and_date(self):
        token, _, _ = cases.stage_pending(self.root, "buyer.eml", HARD.read_bytes())
        cases.confirm_pending(self.root, token)
        case = cases.discover(self.root, refresh=True)[0]
        self.assertTrue(case.customer and "<" not in case.customer)
        self.assertRegex(case.received, r"\d{4}-\d{2}-\d{2}")


class ExtractionReachTest(unittest.TestCase):
    """The path that existed but was unreachable: parser fails -> model extracts."""

    def test_unrecognised_mail_reaches_model_extraction(self):
        extraction = {"document_type": "rfq", "document_confidence": 0.99,
                      "lines": [_line("PN-0001", "8")]}
        with mock.patch.object(perception, "extract", return_value=extraction):
            rfq, report = perception.run(CONFIG, HARD)
        self.assertEqual(report["source"], "agent")
        self.assertEqual(report["mode"], "extract")
        self.assertEqual(report["lines_model"], 1)
        self.assertEqual([line.part_number for line in rfq.lines], ["PN-0001"])

    def test_invented_part_number_is_refused_not_priced(self):
        extraction = {"document_type": "rfq", "document_confidence": 0.99,
                      "lines": [_line("PN-DOES-NOT-EXIST", "8")]}
        with mock.patch.object(perception, "extract", return_value=extraction):
            with self.assertRaises(perception.PerceptionError):
                perception.run(CONFIG, HARD)

    def test_model_failure_says_why_instead_of_faking_a_task(self):
        with mock.patch.object(perception, "extract",
                               side_effect=GenesisError("模型接口不可用")):
            with self.assertRaises(ValueError) as caught:
                web_module._understand_path(HARD, CONFIG)
        message = str(caught.exception)
        self.assertIn("没识别出", message)
        self.assertIn("模型抽取", message)

    def test_missing_model_is_named_not_blamed_on_the_mail(self):
        blank = {"GENESIS_BASE_URL": "", "GENESIS_MODEL": "", "GENESIS_ACCESS_TOKEN": ""}
        with self.assertRaises(ValueError) as caught:
            web_module._understand_path(HARD, blank)
        message = str(caught.exception)
        self.assertIn("模型未配置", message)
        self.assertNotIn("没有可识别的询价表", message)


class MessageTest(unittest.TestCase):
    """What the judge reads on screen."""

    def test_staging_token_never_reaches_the_page(self):
        exc = ValueError("3f9a2b7c8d1e4f5a6b7c8d9e0f1a2b3c.eml: 正文和附件里都没有可识别的询价表")
        self.assertEqual(web_module._upload_reason(exc), "正文和附件里都没有可识别的询价表")

    def test_unrecognised_view_lists_what_the_model_will_read(self):
        view = web_module._unrecognized_view(ROOT, "buyer.eml", HARD)
        self.assertFalse(view["recognized"])
        self.assertTrue(view["readable"])
        self.assertTrue(view["blocks"], "预览要让人看见模型能读到什么")
        self.assertEqual({block["kind"] for block in view["blocks"]},
                         {"body-text", "attachment-xlsx"})

    def test_view_flags_a_missing_model_instead_of_offering_a_dead_button(self):
        with mock.patch.object(web_module, "load_config", return_value={}):
            view = web_module._unrecognized_view(ROOT, "buyer.eml", HARD)
        self.assertFalse(view["model_ready"])
        self.assertIn("模型未配置", view["model_note"])

    def test_empty_mail_preview_is_not_offered_as_extractable(self):
        blank = WORK / "blank.eml"
        blank.parent.mkdir(parents=True, exist_ok=True)
        blank.write_bytes(b"")
        try:
            view = web_module._unrecognized_view(ROOT, "blank.eml", blank)
        finally:
            blank.unlink(missing_ok=True)
        self.assertFalse(view["readable"])
        self.assertFalse(view["model_ready"])


if __name__ == "__main__":
    unittest.main()