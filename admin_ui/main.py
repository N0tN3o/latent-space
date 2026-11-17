#!/usr/bin/env python3
"""
Legacy desktop admin launcher.

The Tkinter Admin Console was retired in favor of the unified Web Command Center (/admin),
which provides live system monitoring, AI & Vision configuration, Kasboek attribution controls,
print management, and log viewing behind admin passcode authentication.
"""

import os
import sys
import webbrowser
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config_manager import get_or_create_admin_password, load_settings


def main():
    settings = load_settings()
    port = int(os.environ.get("PORT", settings.get("server", {}).get("port", 8000)))
    url = f"http://localhost:{port}/admin"
    _, auth_src = get_or_create_admin_password()

    print("=" * 60)
    print("Latent Space Admin has moved to the Web Command Center:")
    print(f"  URL:  {url}")
    print(f"  Auth: {auth_src}")
    print("Start the server with: python start_everything.py")
    print("=" * 60)

    try:
        webbrowser.open(url)
    except Exception:
        pass


if __name__ == "__main__":
    main()
