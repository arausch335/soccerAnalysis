"""Create test fixture parents while resolving operational storage paths."""
from storage.layout import system_path as resolve

def system_path(root,name):
    path=resolve(root,name);path.parent.mkdir(parents=True,exist_ok=True);return path
