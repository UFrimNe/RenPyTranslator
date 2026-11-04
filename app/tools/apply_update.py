"""Entry point copied with update_package.py to an independent temporary Python runtime."""
from pathlib import Path
import sys

if (Path(__file__).parent/'update_package.py').is_file():
    from update_package import main
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
    from rpts.update_package import main

if __name__ == '__main__':
    raise SystemExit(main())
