"""Tests startup failures before C# Main and the recovery archive, without Windows."""
from pathlib import Path
import importlib.util,json,tempfile,unittest,zipfile,sys,subprocess
from unittest.mock import patch
APP=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('rpts_launcher',APP/'tools/launch_desktop.py');launcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(launcher)

class LaunchTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
  for rel in launcher.CRITICAL:
   p=self.root/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'fixture')
  (self.root/'app/VERSION.txt').write_text('test')
 def tearDown(self):self.tmp.cleanup()
 def test_nonzero_native_exit_is_logged_and_reported(self):
  class Failed:
   pid=123
   def wait(self):return -1073741819
  def fake(args,**kwargs):kwargs['stdout'].write(b'Native startup failure\n');return Failed()
  with patch.object(launcher.subprocess,'Popen',fake):
   with self.assertRaisesRegex(RuntimeError,'0xC0000005'):launcher.run_desktop(self.root)
  self.assertIn('0xC0000005',(self.root/'data/logs/launcher.log').read_text())
  self.assertIn('Native startup failure',(self.root/'data/logs/desktop-process.log').read_text())
 def test_zero_exit_is_logged_without_false_failure(self):
  class Successful:
   pid=124
   def wait(self):return 0
  with patch.object(launcher.subprocess,'Popen',return_value=Successful()):self.assertEqual(0,launcher.run_desktop(self.root))
  self.assertIn('process_exit',(self.root/'data/logs/launcher.log').read_text())
 def test_missing_host_file_stops_before_process_launch(self):
  (self.root/'app/desktop/bin/RPTS.Desktop.dll').unlink()
  with patch.object(launcher.subprocess,'Popen') as process:
   with self.assertRaisesRegex(RuntimeError,'RPTS.Desktop.dll'):launcher.run_desktop(self.root)
   process.assert_not_called()
 def test_incorrect_host_hash_stops_before_launch(self):
  manifest={'files':[{'path':'app/desktop/bin/RPTS.Desktop.dll','bytes':7,'sha256':'0'*64}]}
  (self.root/'app/release_manifest.json').write_text(json.dumps(manifest))
  with self.assertRaisesRegex(RuntimeError,'повреждён'):launcher.preflight(self.root)
 def test_diagnostic_run_enables_runtime_trace(self):
  class Process:
   pid=125
   def wait(self):return 0
  with patch.object(launcher.subprocess,'Popen',return_value=Process()) as proc:launcher.run_desktop(self.root,True)
  self.assertEqual('1',proc.call_args.kwargs['env']['COREHOST_TRACE'])
 def test_report_can_be_built_without_gui_and_redacts_tokens(self):
  logs=self.root/'data/logs';logs.mkdir(parents=True)
  (logs/'desktop-process.log').write_text('Bearer verysecret\nRPTS_URL http://127.0.0.1:123/#abcsecret\n"api_key": "othersecret"')
  archive=launcher.startup_report(self.root)
  with zipfile.ZipFile(archive) as z:
   data=z.read('logs/desktop-process.log').decode();self.assertNotIn('verysecret',data);self.assertNotIn('abcsecret',data);self.assertNotIn('othersecret',data)
   self.assertIn('startup_inventory.json',z.namelist())

if __name__=='__main__':unittest.main(verbosity=2)
