"""验证完整 JSON 保存与分层表格读取；不需要数据库或原数据读取服务。"""
from datetime import datetime,timedelta
import json
from pathlib import Path
import tempfile
import unittest

from profiling.statistic import statistic_profile
from profiling.dimension import dimension_profile
from profiling.reader import read_profile


class LayeredReadTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.s=Path(self.temp.name)/'statistic.json';self.d=Path(self.temp.name)/'dimension.json'
        self.cube={'dimensions':[{'name':'channel'}], 'metrics':[{'id':'x','result_column':'x','aggregation':'AVG'}],
                   'time':{'name':'t','grain':'hour','timezone':'Asia/Shanghai'}}

    def tearDown(self):
        self.temp.cleanup()

    def save(self,rows):
        s=statistic_profile(rows,self.cube);d=dimension_profile(rows,self.cube)
        self.s.write_text(json.dumps(s));self.d.write_text(json.dumps(d))
        return s,d

    def read(self,**kwargs):
        return read_profile(self.s,self.d,**kwargs)

    def test_full_storage_10000_values_and_buckets_and_bounded_overview(self):
        rows=[{'channel':str(i),'x':i,'t':(datetime(2026,1,1)+timedelta(hours=i)).isoformat(sep=' ')} for i in range(10000)]
        s,d=self.save(rows)
        self.assertEqual(len(s['time']['buckets']),10000)
        self.assertEqual(len(d['single_dimensions'][0]['groups']),10000)
        self.assertFalse(s['time']['truncated'])
        self.assertFalse(d['single_dimensions'][0]['coverage']['truncated'])
        page=self.read(max_bytes=3000)
        self.assertLessEqual(len((json.dumps(page,ensure_ascii=False,separators=(',',':'))+'\n').encode()),3000)
        self.assertTrue(page['has_more'])
        self.assertGreater(page['returned_count'],0)
        self.assertIn('10000',page['content'])

    def test_index_pagination_and_null_distinction(self):
        rows=[{'channel':v,'x':i,'t':'2026-01-01'} for i,v in enumerate([None,'null','ads','organic','referral'])]
        self.save(rows)
        page=self.read(level='index',dimension=['channel'],limit=2)
        count=page['returned_count'];offsets=[]
        while page['has_more']:
            offsets.append(page['next_offset'])
            page=self.read(level='index',dimension=['channel'],offset=page['next_offset'],limit=2)
            count+=page['returned_count']
        self.assertEqual(count,5)
        self.assertEqual(offsets,[2,4])
        null=self.read(level='detail',dimension=['channel'],values=[None],metrics=['x'],fields=['min','max'])
        self.assertEqual(null['total_count'],1)
        self.assertIn('"type":"null"',null['content'])
        string=self.read(level='detail',dimension=['channel'],values=['null'],fields=['population'])
        self.assertIn('sql_result_rows',string['content'])

    def test_detail_is_exact_saved_distribution_not_recomputed(self):
        rows=[{'channel':'ads','x':x,'t':f'2026-01-01 0{i}:00:00'} for i,x in enumerate([1,10,99])]
        self.save(rows)
        page=self.read(level='detail',dimension=['channel'],values=['ads'],metrics=['x'],fields=['min','p50','max'])
        self.assertIn('| 1 | 10 | 99 |',page['content'])
        page=self.read(level='index',start='2026-01-01 01:00:00',end='2026-01-01 02:00:00')
        self.assertEqual(page['total_count'],1)
        with self.assertRaises(ValueError):
            self.read(level='detail',dimension=['channel'],start='2026-01-01')
        with self.assertRaises(ValueError):
            self.read(metrics=['unknown'])

    def test_empty_and_mutation_free(self):
        self.save([])
        original=(self.s.read_bytes(),self.d.read_bytes())
        page=self.read(level='index',dimension=['channel'])
        self.assertEqual(page['total_count'],0)
        self.assertFalse(page['has_more'])
        self.assertEqual(original,(self.s.read_bytes(),self.d.read_bytes()))
        page=self.read(level='overview')
        self.assertTrue(page['content'].startswith('| 类别 |'))


if __name__=='__main__':
    unittest.main()
