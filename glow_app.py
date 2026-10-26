"""Entry point of GLOW.exe.

    GLOW.exe                  open the settings window
    GLOW.exe daemon           run the overlay daemon
    GLOW.exe on|off|quit      control the glow (for agent hooks)

Build:
    pyinstaller --onefile --noconsole --name GLOW --icon glow.ico glow_app.py
"""
import sys

import glow

if len(sys.argv) > 1:
    glow.main()
else:
    import glow_ui
    glow_ui.main()
