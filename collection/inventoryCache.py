"""Local validation receipts, invalidated by file or identity-rule changes."""
import hashlib
import json
from pathlib import Path
import sqlite3


class InventoryInterrupted(Exception):pass


def fingerprint(path):
    try:
        stat=Path(path).stat()
        return [stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns,stat.st_ino,stat.st_dev]
    except FileNotFoundError:
        return None


def rules_version(root,worker):
    from providers.identityReview import revision
    project=Path(__file__).resolve().parent.parent
    files=['collection/inventoryCache.py','collection/ensembleProgress.py','collection/completion.py','collection/qualifiers.py',
           'collection/whoScoredData.py','collection/legacyFotmobRows.py','collection/selection.py',
           'providers/identity.py','providers/identityReview.py','providers/reep.py',
           'providers/concordance/teams.json','review_data.py','storage/pandasStore.py']
    files.extend(str(p.relative_to(project)) for p in sorted((project/'models').rglob('*.py')))
    files.extend(['providers/fotMobLoader.py','providers/providerLoader.py','providers/matchLoader.py',
                  'providers/events.py','providers/statistics.py'])
    files.append('collection/playerStatsSchema.py')
    digest=hashlib.sha256()
    for name in sorted(set(files)):
        digest.update(name.encode());digest.update((project/name).read_bytes())
    digest.update(json.dumps([str(Path(root).resolve()),worker,revision(root)]).encode())
    return digest.hexdigest()


class InventoryCache:
    def __init__(self,root,worker,*,full=False):
        self.root=Path(root).resolve();self.full=full;self.seen=set()
        self.hits=0;self.checked=0
        self.path=self.root/'_system/cache/inventory.sqlite'
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(self.path)
        try:
            try:self.db.execute('CREATE TABLE IF NOT EXISTS metadata (version TEXT)')
            except sqlite3.DatabaseError as exc:
                if getattr(exc,'sqlite_errorcode',None) not in (sqlite3.SQLITE_CORRUPT,sqlite3.SQLITE_NOTADB):raise
                self.db.close();self.path.unlink();self.db=sqlite3.connect(self.path)
                self.db.execute('CREATE TABLE metadata (version TEXT)')
            self.db.execute('CREATE TABLE IF NOT EXISTS receipts (anchor TEXT PRIMARY KEY, context TEXT, dependencies TEXT, records TEXT)')
            current=self.db.execute('SELECT version FROM metadata').fetchone()
            version=rules_version(self.root,worker)
            if current!=(version,):
                self.db.execute('DELETE FROM receipts');self.db.execute('DELETE FROM metadata')
                self.db.execute('INSERT INTO metadata VALUES (?)',(version,));self.db.commit()
        except BaseException:
            self.db.close();raise

    def key(self,path):
        return Path(path).resolve().relative_to(self.root).as_posix()

    def dependencies(self,paths):
        return {self.key(p):fingerprint(p) for p in paths}

    def unchanged(self,dependencies):
        return all(fingerprint(self.root/path)==stamp for path,stamp in dependencies.items())

    def restore(self,anchor,context=''):
        key=self.key(anchor);self.seen.add(key)
        row=self.db.execute('SELECT context,dependencies,records FROM receipts WHERE anchor=?',(key,)).fetchone()
        if not self.full and row and row[0]==context:
            try:
                dependencies=json.loads(row[1]);records=json.loads(row[2])
                if dependencies and isinstance(records,dict) and self.unchanged(dependencies):
                    self.hits+=1
                    return records
            except (ValueError,TypeError,AttributeError):pass
        self.checked+=1
        # Failed revalidation must never leave an earlier pass active.
        self.db.execute('DELETE FROM receipts WHERE anchor=?',(key,));self.db.commit()
        return None

    def remember(self,anchor,dependencies,records,context=''):
        if not dependencies or not self.unchanged(dependencies):return
        self.db.execute('INSERT OR REPLACE INTO receipts VALUES (?,?,?,?)',
                        (self.key(anchor),context,json.dumps(dependencies),json.dumps(records)))
        self.db.commit()

    def prune(self):
        obsolete=[(key,) for key, in self.db.execute('SELECT anchor FROM receipts') if key not in self.seen]
        self.db.executemany('DELETE FROM receipts WHERE anchor=?',obsolete);self.db.commit()

    def close(self):self.db.close()
