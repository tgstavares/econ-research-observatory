"""Streamlit Community Cloud entry point; maintenance features are unavailable."""
import os
from pathlib import Path
import runpy

root = Path(__file__).resolve().parent
os.environ["ECON_RANKINGS_PUBLIC"] = "1"
os.environ["ECON_RANKINGS_DATABASE"] = str(root / "data/public.sqlite")
os.environ["ECON_RANKINGS_PUBLIC_MANIFEST"] = str(root / "data/manifest.json")
os.environ["ECON_RANKINGS_BOOTSTRAP"] = "0"
runpy.run_path(str(root / "app.py"), run_name="__main__")
