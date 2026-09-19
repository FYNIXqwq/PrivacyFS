"""Validate installed-wheel upgrade, data preservation, rollback and uninstall."""
from pathlib import Path
import argparse
import json
import os
import subprocess
import sys

parser = argparse.ArgumentParser()
parser.add_argument('target', type=Path)
parser.add_argument('bundle', type=Path)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
target, bundle = args.target.resolve(), args.bundle.resolve()
environment = {k:v for k,v in os.environ.items() if k not in {'PYTHONPATH','PYTHONHOME'}}
environment['PIP_CONFIG_FILE'] = os.devnull
def run(command):
    result = subprocess.run([str(v) for v in command], capture_output=True, text=True, timeout=180,
                            env=environment, cwd=target.parent)
    if result.returncode:
        raise RuntimeError('synthetic install check failed: '+result.stdout+result.stderr)
    return json.loads(result.stdout)
def executable():
    slot = (target/'current.txt').read_text()
    return target/'runtimes'/slot/'Scripts/privacyfs.exe'
events=[]
first_slot=(target/'current.txt').read_text()
(target/'data/preserve.db').write_bytes(b'SYNTHETIC_PRESERVE')
events.append(run([sys.executable,bundle/'manage.py','install',target,'--python-exe',sys.executable]))
assert (target/'current.txt').read_text()!=first_slot
health=run([executable(),'doctor','--self-test'])
assert health['self_test']=='passed' and health['container_parser']=='d5-docx-pdf-text-v2'
events.append({'stage':'installed_health','python':health['python'],'version':health['packages']['privacyfs']})
run([executable(),'sample',target/'data/sample'])
source=target/'data/sample/source'
workspace=run([executable(),'workspace','init',source,'--relations',source/'relations.json','--task-profile',source/'profile.json','--state-dir',target/'data/state'])
plan=run([executable(),'workspace','prepare',workspace['workspace_id'],'--state-dir',target/'data/state'])
run([executable(),'review',plan['plan_id'],'--approve',plan['approval_digest'],'--state-dir',target/'data/state'])
published=run([executable(),'export',plan['plan_id'],target/'data/output','--state-dir',target/'data/state'])
assert published['state']=='PUBLISHED'
release_path=target/'data/output'/published['release_id']
events.append({'stage':'installed_trial','state':published['state'],'artifacts':len(list(release_path.iterdir()))})
events.append(run([sys.executable,bundle/'manage.py','rollback',target]))
assert (target/'current.txt').read_text()==first_slot
assert run([executable(),'release','show',published['release_id'],'--state-dir',target/'data/state'])['artifact_integrity']=='verified_against_publication'
events.append(run([sys.executable,bundle/'manage.py','uninstall',target]))
assert (target/'data/preserve.db').read_bytes()==b'SYNTHETIC_PRESERVE'
assert (target/'data/state/.privacyfs-state.json').exists() and release_path.is_dir()
assert not (target/'privacyfs.cmd').exists()
args.output.write_text(json.dumps({'events':events,'state_and_publication_preserved':True},indent=2),encoding='utf-8')
print(json.dumps({'status':'passed','checks':['upgrade','installed_trial','rollback','uninstall'],'data_retained':True}))
