"""Pinned-source build: never inspect a parent/user Git repository."""
from pathlib import Path
Path("FluidNC/src/version.cpp").write_text(
    'const char* grbl_version = "4.1";\n'
    'const char* git_info = "v4.1.1-JD-DGUS2.1-panel6";\n'
    'const char* git_url = "https://github.com/bdring/FluidNC/tree/v4.1.1";\n',
    encoding="utf-8",
)
