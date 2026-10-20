"""Rebuild live ETA from this run's recent reports, including pre-fix workers."""
from copy import deepcopy
from statistics import median


def refresh_timing(runtime):
    runtime=deepcopy(runtime)
    phases=runtime.get('phase_stats',{})
    started=runtime.get('job_started_at','')
    activity=[e for e in runtime.get('activity',[]) if not started or e.get('at','')>=started]
    for key,phase in phases.items():
        selected=[event for event in activity if '|'.join(str(event.get(k,'')) for k in ('competition','season','kind'))==key]
        if phase.get('event')=='job_finished':
            phase.setdefault('finished',True);phase.setdefault('paused',phase.get('exit_code',0)!=0)
        if type(phase.get('cached')) is bool:
            known=[event['cached'] for event in selected if type(event.get('cached')) is int]
            if known:phase['cached']=max(known)+(1 if phase['cached'] and phase.get('status')=='complete' else 0)
        starts={};samples=[]
        for event in selected:
            if event.get('event')=='match_started':starts[event.get('match_id')]=event
            if event.get('event')!='match_saved' or event.get('status') not in ('complete','partial'):continue
            cached=event.get('cached_match',event.get('cached') if type(event.get('cached')) is bool else False)
            if cached:continue
            duration=event.get('active_match_seconds')
            if duration is None and event.get('match_id') in starts:
                duration=event.get('elapsed_seconds',0)-starts[event['match_id']].get('elapsed_seconds',0)
            if type(duration) in (int,float) and duration>0:samples.append(duration)
        phase['processed']=sum(phase.get(k,0) for k in ('scraped','cached','partial','failed','elsewhere') if type(phase.get(k,0)) is int)
        phase['rate_samples']=max(phase.get('rate_samples',0),len(samples[-12:]))
        if phase.get('partial',0):phase['eta_provisional']=True
        rate=phase.get('rate_seconds')
        if rate is None and samples:rate=median(samples[-12:])
        # Historical workers trusted old CSV flags even while revisiting data.
        # Reconstruct a provisional count; new workers report the verified pass.
        if (runtime.get('state')=='scraping' and phase.get('remaining')==0 and
                phase.get('finished') is None and phase.get('total')):
            counts=[phase.get(name,0) for name in ('scraped','cached','failed','partial','elsewhere')]
            processed=sum(value for value in counts if type(value) is int)
            if 0<processed<phase['total']:
                phase['remaining']=phase['total']-processed
                phase['eta_provisional']=True
                phase['remaining_reconstructed']=True
        remaining=phase.get('remaining')
        if runtime.get('state') in ('paused','stopped') or phase.get('paused'):
            phase['eta_seconds']=None
        elif remaining and rate is not None:
            phase['eta_seconds']=rate*remaining
            phase['eta_basis']='current pass; includes partial matches' if phase.get('partial',0) else 'recent matches this run'
        elif remaining and phase.get('eta_seconds',0) and phase['eta_seconds']>0:
            pass
        elif not phase.get('finished'):
            phase['eta_seconds']=None
    current=runtime.get('current')
    if isinstance(current,dict):
        key='|'.join(str(current.get(k,'')) for k in ('competition','season','kind'))
        if key in phases:runtime['current']={**current,**phases[key]}
    return runtime
