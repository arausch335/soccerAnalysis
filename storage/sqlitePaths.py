"""SQLite file URIs that preserve native paths, including Windows long paths."""
import os
from urllib.parse import quote


def readonly_uri(path):
    # Encode before SQLite parses the URI. In particular, \\?\ Windows paths
    # must remain filesystem paths, never turn into a file:// URI authority.
    # Encoding separators also preserves UNC paths without a remote authority.
    return 'file:' + quote(os.fspath(path), safe='') + '?mode=ro'
