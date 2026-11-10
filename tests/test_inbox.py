"""Inbox behaviour: the demo ships with one sample, the rest are admitted by the user."""

import shutil
import json
import unittest
from pathlib import Path

from rfq_agent.application import cases
from rfq_agent.application.cases import SEED, admit_case, available, discover, inbox, mark_stage

ROOT = Path(__file__).resolve().parents[1]
MATERIAL = ROOT / "data" / "询价信息素材"
WORK = ROOT / "runtime" / "test_inbox"


def _copy(target: Path):
    """Copy the B and C mails (their attachments live inside the .eml) into a scratch root."""
    destination = target / "data" / "询价信息素材" / "1"
    destination.mkdir(parents=True, exist_ok=True)
    for source in (MATERIAL / "1").glob("*.eml"):
        if "20260902" in source.name or "Spare Parts" in source.name:
            shutil.copyfile(source, destination / source.name)


class InboxTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        shutil.rmtree(WORK, ignore_errors=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(WORK, ignore_errors=True)

    def setUp(self):
        # One root per test: a fresh database, no state shared through the file system.
        self.root = WORK / self._testMethodName
        shutil.rmtree(self.root, ignore_errors=True)
        _copy(self.root)
        cases._CACHE.clear()

    def keys(self):
        return [item["case"].key for item in inbox(self.root)]

    def test_material_has_the_two_copied_mails(self):
        self.assertEqual([case.key for case in discover(self.root, refresh=True)], ["B", "C"])

    def test_first_run_seeds_only_the_declared_sample(self):
        self.assertEqual(SEED, ("B",))
        self.assertEqual(self.keys(), ["B"])

    def test_seeding_is_idempotent(self):
        self.keys()
        self.assertEqual(self.keys(), ["B"])

    def test_available_is_everything_not_yet_admitted(self):
        self.keys()
        self.assertEqual([case.key for case in available(self.root)], ["C"])

    def test_admitting_moves_a_case_from_pool_to_inbox(self):
        self.keys()
        admit_case(self.root, "C")
        self.assertEqual(self.keys(), ["B", "C"])
        self.assertEqual(available(self.root), [])

    def test_admitting_twice_does_not_duplicate(self):
        admit_case(self.root, "C")
        admit_case(self.root, "C")
        self.assertEqual(self.keys(), ["B", "C"])

    def test_stage_starts_received_and_advances(self):
        admit_case(self.root, "C")
        item = next(row for row in inbox(self.root) if row["case"].key == "C")
        self.assertEqual(item["stage"], "received")
        self.assertIsNone(item["task_id"])
        # A stage only means something while the task it names is on disk.
        folder = self.root / "runtime" / ("c" * 32)
        folder.mkdir(parents=True)
        (folder / "task.json").write_text(json.dumps({"revision": 1, "status": "analyzed"}))
        mark_stage(self.root, "C", "priced", "c" * 32)
        item = next(row for row in inbox(self.root) if row["case"].key == "C")
        self.assertEqual(item["stage"], "priced")
        self.assertEqual(item["task_id"], "c" * 32)

    def test_task_lost_with_the_container_is_not_advertised(self):
        """After a free-tier restart the row survives only in memory of a dead task.

        Reporting that id is what made the desk say "已有任务" and then open blank.
        """
        admit_case(self.root, "C")
        mark_stage(self.root, "C", "priced", "d" * 32)      # no directory on disk
        item = next(row for row in inbox(self.root) if row["case"].key == "C")
        self.assertIsNone(item["task_id"])
        self.assertEqual(item["stage"], "received")
        self.assertEqual(item["history"], [])

    def test_unknown_case_is_rejected(self):
        with self.assertRaises(ValueError):
            admit_case(self.root, "Z")

    def test_identical_uploaded_mail_groups_without_losing_tasks(self):
        source = cases.case_by_key(self.root, 'B').path
        target = self.root / cases.UPLOADS / 'copy.eml'
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source,target)
        discover(self.root,refresh=True)
        admit_case(self.root,'B'); admit_case(self.root,'UP-copy')
        for key,tid,at in [('B','a'*32,'2026-11-01'),('UP-copy','b'*32,'2026-11-02')]:
            folder=self.root/'runtime'/tid;folder.mkdir(parents=True)
            (folder/'desk.json').write_text(json.dumps({'revision':2,'status':'awaiting_review','agent_trace':[{'action':'task_created','at':at}]}))
            mark_stage(self.root,key,'priced',tid)
        groups=inbox(self.root)
        self.assertEqual(len(groups),1)
        self.assertEqual(len(groups[0]['history']),2)
        self.assertEqual(groups[0]['task_id'],'b'*32)
        self.assertTrue(target.exists())
        token,_,_=cases.stage_pending(self.root,'another.eml',source.read_bytes())
        self.assertEqual(cases.confirm_pending(self.root,token),source)
        self.assertFalse((self.root/cases.UPLOADS/'another.eml').exists())

    def test_reset_returns_to_the_first_run_state(self):
        """Between two judges the demo must be able to go back to the sample only."""
        admit_case(self.root, "C")
        mark_stage(self.root, "C", "priced", "task-123")
        cases.reset_inbox(self.root)
        self.assertEqual(self.keys(), ["B"])
        item = inbox(self.root)[0]
        self.assertEqual(item["stage"], "received")
        self.assertIsNone(item["task_id"])
        self.assertEqual([case.key for case in available(self.root)], ["C"])


if __name__ == "__main__":
    unittest.main()
