"""Entry point for the PyInstaller build.

PyInstaller executes its entry script as ``__main__`` with no package context, so
a module that uses relative imports (every module inside ``eteq``) cannot be the
entry point directly. This shim imports the package normally instead.
"""

import sys

from eteq.cli import main

if __name__ == "__main__":
    sys.exit(main())
