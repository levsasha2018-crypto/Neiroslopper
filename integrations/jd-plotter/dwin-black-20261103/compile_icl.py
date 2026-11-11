"""Reuse the saved official DWIN ICL compiler without duplicating its tools."""
import importlib.util
from pathlib import Path

source = Path(__file__).resolve().parent.parent / "dwin-ui-20261021/compile_icl.py"
spec = importlib.util.spec_from_file_location("saved_official_icl", source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
compile_icl = module.compile_icl
