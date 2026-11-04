from __future__ import annotations
import argparse, json, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from rpts.local_ai import *

def report():
    hw=detect_hardware(); rec=recommend_model(hw)
    value={
      'hardware': {'os':hw.os_name,'arch':hw.arch,'cpu':hw.cpu,'logical_cores':hw.logical_cores,'ram_bytes':hw.ram_bytes,
                   'gpus':[asdict(g) for g in hw.gpus]},
      'recommended_backend':recommended_backend(hw),'recommended_model':rec.key,
      'models':[{**asdict(m),'required_free_bytes':required_free_bytes(m)} for m in MODEL_SPECS.values()],
      'storage':enumerate_storage_locations(),
    }
    print(json.dumps(value,ensure_ascii=False,indent=2))

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('command',nargs='?',default='hardware')
    a=ap.parse_args()
    if a.command=='hardware': report(); return 0
    raise SystemExit('unknown command')
if __name__=='__main__': raise SystemExit(main())
