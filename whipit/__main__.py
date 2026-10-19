"""Module execution entrypoint: python -m whipit."""

import sys
from whipit.ui.gui.app import main

if __name__ == "__main__":
    sys.exit(main())
