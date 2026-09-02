import tempfile, unittest
from pathlib import Path
from novel_agent.publishers import DryRunPublisher, LocalFilePublisher

class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.chapter={'id':'c1','number':1,'title':'标题','content':'正文','review':{'passed':True,'blockingIssues':[]}}
        self.novel={'id':'n1','title':'小说','story_bible_version':1}
        self.tmp=tempfile.TemporaryDirectory()
    def tearDown(self): self.tmp.cleanup()
    def test_dry_run_has_no_side_effect(self): self.assertFalse(DryRunPublisher().publish(self.chapter)['sideEffect'])
    def test_local_file_publisher_exports_only_local_file(self):
        result=LocalFilePublisher(Path(self.tmp.name)).publish(self.chapter,self.novel); self.assertEqual(result['status'],'EXPORTED'); self.assertTrue(Path(result['path']).exists()); self.assertEqual(result['sideEffect'],'local_file_only')

if __name__=='__main__': unittest.main()
