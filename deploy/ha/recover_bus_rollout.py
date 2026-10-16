"""Explicit local recovery phases: held reserves, release, owner last, verify."""
from __future__ import annotations
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'deploy/ha')]
from deploy.ha import release_update as update


def emit(kind, **fields):
    print(json.dumps(dict(step=kind, **fields), ensure_ascii=False), flush=True)


def options(args):
    args.prepare = args.apply = False
    args.recover_reserve = False
    args.pin_release = args.refresh_bootstrap = args.replace_pinned_trial = True
    args.handoff = args.hold_maintenance = False
    return args


def maintenance(cfg, token, enabled, release):
    own = next(n for n in cfg['nodes'] if n['id'] == cfg['node_id'])
    code, result = update.json_request(own['url'].rstrip('/')+'/maintenance', token,
        body={'enabled':enabled, 'release':release}, timeout=10)
    if code != 200 or result.get('operator_maintenance') is not enabled:
        raise RuntimeError('LocalMaintenanceNotConfirmed')


def stage_held(args, cfg, env):
    token, own = env['PERIMETER_HA_TOKEN'], cfg['node_id']
    rows, owner = update.inventory(cfg, token)
    if own == owner:
        raise RuntimeError('LocalNodeIsActive:held_stage_requires_passive_node; no services changed')
    row = rows[own]
    if (row.get('release_sha') == args.release and update.fresh_node(row, own)
            and row.get('update', {}).get('operator_pinned') is True
            and row.get('auto_failback') is False
            and row.get('operator_maintenance') is True
            and row.get('preflight', {}).get('ok') is True):
        result = dict(installed=True, already_installed=True, node=own,
                      maintenance_held=True, workers_started=False)
        emit('HELD_NODE_INSTALLED', **result)
        return result
    old_release = rows[own]['release_sha']
    was_held = rows[own].get('operator_maintenance') is True
    # This SQL-checked endpoint refuses a concurrent acquisition by this node.
    # Holding before preparation prevents failback and moving trial receipts.
    try:
        maintenance(cfg, token, True, old_release)
        emit('PREPARING_HELD_NODE', node=own, active_owner=owner)
        update.prepare(args, cfg, env)
        args.hold_maintenance = True
        result = update.apply(args, cfg, env)
    except Exception:
        # An unfinished native recovery journal must be inspected/recovered first.
        if not was_held and not (Path(cfg['state_dir'])/'operator-release-install.json').exists():
            maintenance(cfg, token, False, old_release)
        raise
    emit('HELD_NODE_INSTALLED', **result)
    return result


def release_pair(args, cfg, env):
    result = update.release_reserves(args, cfg, env)
    emit('PAIR_RELEASED', **result)
    deadline = time.monotonic()+240
    while True:
        try:
            rows, owner = update.inventory(cfg, env['PERIMETER_HA_TOKEN'])
            ready = all(rows[key].get('prepared') is True
                and rows[key].get('faulted') is False
                and rows[key].get('operator_maintenance') is False
                and rows[key].get('release_sha') == args.release
                and (not rows[key].get('active') or rows[key].get('healthy') is True)
                for key in result['nodes'])
            if ready:
                emit('PAIR_READY', nodes=result['nodes'], active_owner=owner)
                return result
        except Exception:
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError('ReleasedPairNotReady:no_automatic_rollback')
        emit('WAITING_FOR_PAIR_VERIFICATION')
        time.sleep(5)


def finish(args, cfg, env):
    if cfg['node_id'] != 'perimetr':
        raise RuntimeError('FinishMustRunOnPerimetr')
    rows, owner = update.inventory(cfg, env['PERIMETER_HA_TOKEN'])
    if rows[cfg['node_id']].get('release_sha') == args.release:
        emit('LAST_NODE_ALREADY_INSTALLED', node=cfg['node_id'])
        return {'installed':True, 'already_installed':True}
    update.require_reserves(rows, cfg['node_id'], args.release)
    emit('PREPARING_LAST_NODE', node=cfg['node_id'], active_owner=owner)
    update.prepare(args, cfg, env)
    args.handoff = True
    result = update.apply(args, cfg, env)
    emit('LAST_NODE_INSTALLED', **result)
    return result


def acceptance(rows, release):
    active = [key for key, row in rows.items() if row.get('active') is True]
    owner = active[0] if len(active) == 1 else None
    good = len(rows) == 3 and owner is not None
    for key, row in rows.items():
        good = good and (update.fresh_node(row, key)
            and row.get('release_sha') == release and row.get('prepared') is True
            and row.get('faulted') is False and row.get('operator_maintenance') is False
            and (row.get('update', {}).get('operator_pinned') is True
                or (row.get('auto_update') is True
                    and row.get('update', {}).get('qualification') == 'passed'))
            and row.get('auto_failback') is False)
    if owner:
        row = rows[owner]
        health = row.get('services', {})
        metrics = row.get('bus_metrics', {})
        age = row.get('bus_sample_age')
        good = good and (row.get('healthy') is True and len(health) == 5
            and all(service.get('ok') is True for service in health.values())
            and row.get('update', {}).get('pending') is False
            and type(age) in (int, float) and 0 <= age <= 15
            and all(type(metrics.get(k)) is int and metrics[k] >= 0 for k in
                ('rfid_pending', 'rfid_sent', 'video_pending', 'video_sent')))
    mirrors = all(row.get('metadata_mirror', {}).get('enabled') is True
                  and not row.get('metadata_mirror', {}).get('error')
                  and not row.get('metadata_mirror', {}).get('blocked_streams')
                  for row in rows.values())
    return bool(good), owner, mirrors


def verify(args, cfg, env):
    deadline, stable, identity = time.monotonic()+600, 0, None
    stable_since = None
    while True:
        try:
            rows, _ = update.inventory(cfg, env['PERIMETER_HA_TOKEN'])
            good, owner, mirrors = acceptance(rows, args.release)
            epochs = {row.get('epoch') for row in rows.values()}
            new_identity = (owner, next(iter(epochs))) if len(epochs) == 1 else None
            good = good and new_identity is not None
            stable = stable+1 if good and identity == new_identity else int(good)
            if not good:
                stable_since = None
            elif identity != new_identity or stable_since is None:
                stable_since = time.monotonic()
            identity = new_identity
            stable_sec = time.monotonic()-stable_since if stable_since is not None else 0
            summary = {key:{field:row.get(field) for field in
                ('release_sha','active','healthy','prepared','faulted','epoch')}
                for key,row in rows.items()}
            emit('VERIFYING', nodes=summary, accepted_samples=stable, stable_seconds=round(stable_sec, 1),
                 mirrors_without_reported_errors=mirrors)
            if stable >= 3 and stable_sec >= 120:
                opener = build_opener(ProxyHandler({}))
                with opener.open(Request('http://172.31.0.97:5051/health/ready'), timeout=5) as response:
                    gateway = response.status == 200
                if gateway:
                    result = dict(core_ha_accepted=True, owner=owner, epoch=identity[1],
                        gateway_ready=True, stable_seconds=round(stable_sec, 1), mirrors_without_reported_errors=mirrors,
                        long_term_stability='not_yet_measured', physical_business_acceptance='not_measured')
                    emit('CORE_HA_ACCEPTED', **result)
                    return result
        except Exception as exc:
            stable, identity, stable_since = 0, None, None
            emit('WAITING_FOR_READINESS', error=type(exc).__name__)
        if time.monotonic() >= deadline:
            raise RuntimeError('LiveAcceptanceNotConfirmed:no_automatic_rollback; inspect_reported_status')
        time.sleep(5)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', choices=('hold','release','finish','verify'), required=True)
    parser.add_argument('--release', required=True)
    parser.add_argument('--config', type=Path, default=Path(
        'D:/PerimeterHA/node.json' if os.name == 'nt' else '/etc/perimeter/node.json'))
    parser.add_argument('--environment', type=Path, default=Path(
        'D:/PerimeterHA/transfer-private/environment.local.json' if os.name == 'nt'
        else '/etc/perimeter/environment'))
    args = options(parser.parse_args(argv))
    if not re.fullmatch(r'[0-9a-f]{40}', args.release):
        raise ValueError('ExactReleaseRequired')
    if not (bool(__import__('ctypes').windll.shell32.IsUserAnAdmin()) if os.name == 'nt' else os.geteuid() == 0):
        raise RuntimeError('AdministratorRequired')
    cfg = json.loads(args.config.read_text(encoding='utf-8-sig'))
    if (os.name == 'nt') != (cfg['node_id'] == 'physical'):
        raise RuntimeError('DeploymentOperatingSystemMismatch')
    if update.git(cfg,'-C',ROOT,'rev-parse','HEAD') != args.release:
        raise RuntimeError('ToolCheckoutDoesNotMatchRelease')
    if update.git(cfg,'-C',ROOT,'status','--porcelain'):
        raise RuntimeError('CleanToolsCheckoutRequired')
    env = update.load_environment(args, cfg)
    os.environ.update(env)
    from common.single_instance import SingleInstanceLock
    with SingleInstanceLock(str(Path(cfg['state_dir'])/'operator-update.lock')):
        if args.phase == 'hold':
            stage_held(args,cfg,env)
        elif args.phase == 'release':
            release_pair(args,cfg,env)
        elif args.phase == 'finish':
            finish(args,cfg,env)
            verify(args,cfg,env)
        else:
            verify(args,cfg,env)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        from deploy.ha.diagnose_fault import clean
        message = str(exc) if type(exc) in (ValueError,RuntimeError) else type(exc).__name__
        print('RECOVERY_PHASE_FAILED', clean(message,os.environ), file=sys.stderr, flush=True)
        raise SystemExit(2)
