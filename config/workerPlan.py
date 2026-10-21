"""One Git-shared plan assigns jobs to user-named workers."""
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re

import yaml
import config
from collection.selection import competition_by_name, season_key, season_label
from collection.workerIdentity import valid_worker_id

TYPES={'combined'}
RESOURCES={
 'sofascore_stats':('Sofascore · player statistics','sofascore'),
 'sofascore_details':('Sofascore · match detail bundle','sofascore'),
 'match_statistics':('Sofascore · team match statistics','sofascore'),
 'incidents':('Sofascore · match incidents','sofascore'),
 'momentum':('Sofascore · momentum graph','sofascore'),
 'average_positions':('Sofascore · average positions','sofascore'),
 'heatmaps':('Sofascore · player heatmaps','sofascore'),
 'shotmaps':('Sofascore · shot maps','sofascore'),
 'action_maps':('Sofascore · pass, dribble & defence maps','sofascore'),
 'whoscored_events':('WhoScored · full event stream','whoscored_events'),
 'whoscored_stats':('WhoScored · player statistics enrichment','sofascore'),
 'fotmob_enrichment':('FotMob · player statistics enrichment','sofascore'),
 'fotmob_stats':('FotMob · player statistics','fotmob_players'),
}


def requested_resources(types,spatial):
    result=set()
    if set(types)&{'combined','sofascore'}:
        result.update(('sofascore_stats','sofascore_details','match_statistics','incidents','momentum','average_positions'))
        if spatial=='all':result.update(('heatmaps','shotmaps','action_maps'))
    if 'combined' in types:result.update(('whoscored_stats','fotmob_enrichment'))
    if set(types)&{'combined','whoscored_events'}:result.add('whoscored_events')
    if 'fotmob_players' in types:result.add('fotmob_stats')
    return sorted(result)


@dataclass(frozen=True)
class Job:
    id:str
    assignment:str
    worker:str
    competition:str
    season:str
    collection_types:tuple
    spatial_capture:str

    @property
    def resources(self):return requested_resources(self.collection_types,self.spatial_capture)

    def args(self,root):
        return ['--competition',self.competition,'--season',self.season,'--data-root',str(root),
                '--collection-types',*self.collection_types,'--sofascore-spatial-capture',self.spatial_capture]

    def as_dict(self):
        return {'id':self.id,'assignment':self.assignment,'worker':self.worker,'competition':self.competition,
                'season':season_label(self.season),'collection_types':list(self.collection_types),
                'spatial_capture':self.spatial_capture,'resources':self.resources}


@dataclass
class WorkerPlan:
    workers:dict
    jobs:list
    git:dict
    dashboard:dict
    reporting:dict
    storage:dict
    coordinator:str
    revision:str
    path:Path

    def for_worker(self,worker):
        if worker not in self.workers:raise config.ConfigError(f'Worker {worker!r} is not in config/workers.yaml')
        return [job for job in self.jobs if job.worker==worker] if self.workers[worker]['enabled'] else []


def section(value,keys,label):
    return config._section(value,keys,label)


def load_plan(path=None):
    path=Path(path or config.PROJECT_ROOT/'config/workers.yaml')
    try:
        data=path.read_bytes()
        document=yaml.load(data.decode('utf-8'),Loader=config._UniqueKeyLoader)
    except (OSError,yaml.YAMLError,UnicodeError) as exc:
        raise config.ConfigError(f'Cannot load worker plan: {exc}') from exc
    if not isinstance(document,dict):raise config.ConfigError('workers plan must be a YAML mapping')
    document.setdefault('reporting',{'enabled':False,'heartbeat_seconds':30})
    document.setdefault('storage',{'enabled':False,'hub':'msi','url':'http://127.0.0.1:8767','consumers':['main'],'publish':'season'})
    section(document,('version','coordinator','git','dashboard','workers','reporting','storage'),'workers plan')
    storage=section(document['storage'],('enabled','hub','url','consumers','publish'),'workers.storage')
    config._boolean(storage['enabled'],'workers.storage.enabled')
    if storage['publish']!='season':raise config.ConfigError('Storage datasets publish after completed seasons')
    from collection.reporting import validate_url
    storage['url']=validate_url(storage['url'])
    valid_worker_id(storage['hub'])
    if not isinstance(storage['consumers'],list) or not storage['consumers']:raise config.ConfigError('Define dataset consumers')
    for consumer in storage['consumers']:valid_worker_id(consumer)
    reporting=section(document['reporting'],('enabled','heartbeat_seconds'),'workers.reporting')
    config._boolean(reporting['enabled'],'workers.reporting.enabled')
    if type(reporting['heartbeat_seconds']) is not int or not 10<=reporting['heartbeat_seconds']<=300:raise config.ConfigError('Reporting heartbeat must be 10–300 seconds')
    if type(document['version']) is not int or document['version']!=1:raise config.ConfigError('workers plan version must be 1')
    git=section(document['git'],('enabled','remote','branch','poll_seconds','auto_restart'),'workers.git')
    for field in ('enabled','auto_restart'):config._boolean(git[field],'workers.git.'+field)
    if not isinstance(git['remote'],str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*',git['remote']):raise config.ConfigError('Invalid Git remote')
    if (not isinstance(git['branch'],str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]*',git['branch'])
            or '..' in git['branch'] or git['branch'].endswith(('/','.lock'))):raise config.ConfigError('Invalid Git branch')
    if type(git['poll_seconds']) is not int or git['poll_seconds']<30:raise config.ConfigError('Git polling must be at least 30 seconds')
    dashboard=section(document['dashboard'],('enabled','port','open_browser'),'workers.dashboard')
    for field in ('enabled','open_browser'):config._boolean(dashboard[field],'workers.dashboard.'+field)
    if type(dashboard['port']) is not int or not 1024<=dashboard['port']<=65535:raise config.ConfigError('Dashboard port must be 1024–65535')
    if not isinstance(document['workers'],dict) or not document['workers']:raise config.ConfigError('Define at least one worker')
    workers={};jobs=[];seen={}
    for worker,value in document['workers'].items():
        valid_worker_id(worker)
        if isinstance(value,dict):
            value.setdefault('role','monitor' if worker==document['coordinator'] else 'worker')
            value.setdefault('dashboard',worker==document['coordinator'])
        section(value,('label','color','enabled','assignments','role','dashboard'),'worker '+worker)
        if value['role'] not in ('monitor','worker'):raise config.ConfigError('Worker role must be monitor or worker')
        config._boolean(value['dashboard'],'worker dashboard')
        config._boolean(value['enabled'],'worker enabled')
        if not isinstance(value['label'],str) or not value['label'].strip():raise config.ConfigError('Worker labels must be nonempty')
        if not isinstance(value['color'],str) or not re.fullmatch(r'#[0-9a-fA-F]{6}',value['color']):raise config.ConfigError('Worker colors must be #RRGGBB')
        if not isinstance(value['assignments'],list):raise config.ConfigError('Worker assignments must be a list')
        workers[worker]=value;ids=set()
        for assignment in value['assignments']:
            section(assignment,('id','competitions','seasons','collection_types','spatial_capture'),'assignment')
            aid=assignment['id']
            if not isinstance(aid,str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}',aid) or aid in ids:raise config.ConfigError('Assignment IDs must be unique within each worker')
            ids.add(aid)
            types=config._strings(assignment['collection_types'],'assignment collection_types')
            if set(types)-TYPES or {'combined','sofascore'}<=set(types):raise config.ConfigError('Invalid assignment collection types')
            spatial=assignment['spatial_capture']
            if spatial not in ('fast','all'):raise config.ConfigError('Assignment spatial_capture must be fast or all')
            competitions=[competition_by_name(v).name for v in config._strings(assignment['competitions'],'assignment competitions')]
            seasons=[season_key(v) for v in config._strings(assignment['seasons'],'assignment seasons')]
            for competition in competitions:
                for season in seasons:
                    jid=worker+':'+aid+':'+re.sub('[^a-z0-9]+','-',competition.lower())+':'+season_label(season)
                    job=Job(jid,aid,worker,competition,season,tuple(types),spatial)
                    if value['enabled']:
                        for resource in job.resources:
                            key=(competition,season,resource)
                            if key in seen:raise config.ConfigError(f'Overlapping assignment: {competition} {season_label(season)} {resource} ({seen[key]} and {worker})')
                            seen[key]=worker
                    jobs.append(job)
    if document['coordinator'] not in workers:raise config.ConfigError('Coordinator must be a configured worker')
    if storage['enabled']:
        if not reporting['enabled']:raise config.ConfigError('Storage setup requires direct reporting')
        from urllib.parse import urlsplit
        from collection.reporting import private_ip
        address=urlsplit(storage['url'])
        if address.scheme!='http' or not address.port or not private_ip(address.hostname):raise config.ConfigError('Storage requires an explicit private IPv4 HTTP address and port')
        if storage['hub'] not in workers or any(w not in workers for w in storage['consumers']):raise config.ConfigError('Storage workers must be configured')
        if storage['hub'] in storage['consumers']:raise config.ConfigError('Storage hub cannot be its own dataset consumer')
    return WorkerPlan(workers,jobs,dict(git),dict(dashboard),dict(reporting),dict(storage),document['coordinator'],hashlib.sha256(data).hexdigest(),path)
