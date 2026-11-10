import json
import shutil
import unittest
from pathlib import Path
from uuid import uuid4
from rfq_agent.application.cases import load_case, provider_for
from rfq_agent.application.service import RFQService
from rfq_agent.infrastructure.storage import TaskStore
from rfq_agent.web import _review_page

ROOT = Path(__file__).parents[1]


class ReviewPreviewTests(unittest.TestCase):
    def setUp(self):
        self.path = ROOT / 'runtime/tests' / ('review-preview-' + uuid4().hex)
        self.path.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.path)
        self.service = RFQService(TaskStore(self.path), provider=provider_for(ROOT))
        rfq, _ = load_case(ROOT, 'B')
        self.task = self.service.start_task(rfq.to_dict())
        self.tid = self.task['task_id']
        self.item = next(i for i in self.task['review_items'] if any(o['id']=='quote_with_moq' for o in i['options']))

    def test_preview_is_read_only_and_matches_commit(self):
        file = self.path / self.tid / 'task.json'
        before = file.read_bytes()
        preview = self.service.preview_review(self.tid,1,self.item['id'],'quote_with_moq')
        self.assertEqual(before,file.read_bytes())
        result=self.service.resolve_review(self.tid,1,self.item['id'],'quote_with_moq', '已核对')
        self.assertEqual(result['revision'],2)
        self.assertEqual(result['decision_log'][-1]['effect']['after'],preview['after'])
        self.assertEqual(result['rfq'],self.task['rfq'])

    def test_amend_keeps_history_and_vanished_item(self):
        result=self.service.resolve_review(self.tid,1,self.item['id'],'request_quotation')
        page=_review_page(result,'passed',0,80)
        saved=next(i for i in page['items'] if i['id']==self.item['id'])
        self.assertEqual(saved['history'][0]['option'],'request_quotation')
        with self.assertRaises(ValueError):
            self.service.resolve_review(self.tid,2,self.item['id'],'quote_with_moq',amend=True)
        preview=self.service.preview_review(self.tid,2,self.item['id'],'quote_with_moq',True)
        result=self.service.resolve_review(self.tid,2,self.item['id'],'quote_with_moq','重新核对 MOQ',True)
        self.assertEqual(result['revision'],3)
        self.assertEqual(len(result['decision_log']),2)
        self.assertEqual(result['decision_log'][-1]['previous_option'],'request_quotation')
        self.assertEqual(result['decision_log'][-1]['effect']['after'],preview['after'])
        self.assertEqual(result['rfq'],self.task['rfq'])
        saved=next(i for i in _review_page(result,'passed',0,80)['items'] if i['id']==self.item['id'])
        self.assertEqual(len(saved['history']),2)

    def test_invalid_stale_and_active_rejected(self):
        for rev,option in [(9,'request_quotation'),(1,'invented')]:
            with self.assertRaises(ValueError):
                self.service.preview_review(self.tid,rev,self.item['id'],option)
        task=self.service.store.get(self.tid)
        task.status='active'
        self.service.store.save(task)
        with self.assertRaises(ValueError):
            self.service.resolve_review(self.tid,1,self.item['id'],'request_quotation')


if __name__=='__main__': unittest.main()
