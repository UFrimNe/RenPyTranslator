"""Run the source-only regression suite without bundled runtimes or a model."""
from pathlib import Path
import sys
import unittest

APP = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(APP / 'src'), str(APP / 'tools'), str(APP / 'tests')]
MODULES = (
    'test_automatic_gender', 'test_gender_review', 'test_scene_situation',
    'test_desktop', 'test_translation_repairs', 'test_launch_and_static_ui',
    'test_native_folder', 'test_launcher',
)


def main():
    suite = unittest.defaultTestLoader.loadTestsFromNames(MODULES)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
