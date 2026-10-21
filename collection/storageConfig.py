"""Main provisions separate storage credentials through authenticated reporting."""
import json
from pathlib import Path
import secrets
from urllib.parse import urlsplit

from storage.pandasStore import atomic_json
from .reporting import private_ip,validate_url
from .workerIdentity import valid_worker_id


def path_for(root):return Path(root)/'_system/config/storage.json'


def install(root,value):
    if value.get('role') not in ('hub','producer','consumer'):raise ValueError('Invalid storage role')
    valid_worker_id(value['worker_id']);valid_worker_id(value['hub'])
    validate_url(value['url'])
    if value['role']=='hub':
        host=urlsplit(value['url']).hostname
        if not private_ip(host):raise ValueError('Bind storage to the MSI private/Tailscale IP')
        for worker,credential in value['tokens'].items():
            valid_worker_id(worker)
            if credential['role'] not in ('producer','consumer') or len(credential['token'])<32:raise ValueError('Invalid storage credentials')
    elif not isinstance(value.get('token'),str) or len(value['token'])<32:raise ValueError('Missing storage token')
    path=path_for(root)
    if not path.exists() or json.loads(path.read_text())!=value:atomic_json(path,value)
    path.chmod(0o600)


class Provisioner:
    def __init__(self,root,settings,workers):
        self.settings=settings;self.workers=workers
        path=Path(root)/'_system/config/storage-secrets.json'
        self.tokens=json.loads(path.read_text()) if path.exists() else {}
        for worker in workers:
            if worker not in self.tokens:self.tokens[worker]=secrets.token_urlsafe(40)
        atomic_json(path,self.tokens);path.chmod(0o600)

    def for_worker(self,worker):
        if not self.settings.get('enabled') or worker not in self.workers:return None
        hub=self.settings['hub'];consumers=self.settings['consumers']
        base={'hub':hub,'url':self.settings['url'],'worker_id':worker,'publish':'season'}
        if worker==hub:
            return {**base,'role':'hub','tokens':{w:{'token':self.tokens[w],'role':'consumer' if w in consumers else 'producer'}
                    for w in self.workers if w!=hub}}
        return {**base,'role':'consumer' if worker in consumers else 'producer','token':self.tokens[worker]}
