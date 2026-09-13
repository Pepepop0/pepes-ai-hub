"""PyInstaller entry script: the same ``main`` the ``local-ai-stack`` command runs.

PyInstaller executes its entry file as a top-level script, where the
package's own ``__main__.py`` relative import has no parent. This file exists
only to spell the import absolutely; there is no second application flow.
"""

import sys

from local_ai_stack.cli import main

sys.exit(main())
