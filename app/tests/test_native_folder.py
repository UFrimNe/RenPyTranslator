from pathlib import Path
import sys,tempfile,unittest,json,subprocess
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from rpts import native_folder

class NativeFolderTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
  for name in ['runtime/dotnet/dotnet.exe','app/desktop/bin/RPTS.FolderPicker.dll']:
   p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'fixture')
  self.fake_os=SimpleNamespace(name='nt',environ={},getppid=lambda:123)
 def tearDown(self):self.temp.cleanup()
 def pick(self,reply,code=0,kind='game'):
  with patch.object(native_folder,'os',self.fake_os),patch.object(native_folder,'parent_window',return_value=77),patch.object(native_folder.subprocess,'run',return_value=subprocess.CompletedProcess([],code,json.dumps(reply),'test stderr')) as run:
   result=native_folder.pick_folder(self.root,{'kind':kind,'initial':"C:\\Игра 'тест'"})
   self.assertEqual(77,json.loads(run.call_args.kwargs['input'])['owner'])
   return result
 def test_cancel_returns_null(self):self.assertIsNone(self.pick({'path':None})['path'])
 def test_unicode_folder_survives_json(self):self.assertEqual("C:\\Игры\\Тест 'Игра'",self.pick({'path':"C:\\Игры\\Тест 'Игра'"})['path'])
 def test_native_failure_is_logged_without_crashing_host(self):
  with self.assertRaisesRegex(ValueError,'вручную'):self.pick({},1)
  self.assertIn('test stderr',(self.root/'data/logs/native-folder.log').read_text())
 def test_invalid_kind_never_launches_process(self):
  with patch.object(native_folder,'os',self.fake_os),patch.object(native_folder.subprocess,'run') as run:
   with self.assertRaises(ValueError):native_folder.pick_folder(self.root,{'kind':'shell','initial':''})
   run.assert_not_called()

if __name__=='__main__':unittest.main(verbosity=2)
