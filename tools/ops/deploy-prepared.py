"""Activate only a root-owned, verified release prepared on this host.

Never builds, pulls arbitrary images, edits production source, handles credentials,
or touches Mongo/volumes. Rollback uses the precise prior Compose configuration.
"""
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.request import urlopen

ROOT = Path('/opt/ethinx/facelessforge-ops')
CONTAINER = 'facelessforge-app'
PROJECT = 'ff-continuity'


def run(args, timeout=120):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        # Docker/Compose errors can contain secrets; retain only the exit code.
        raise RuntimeError(f'Command failed with exit {result.returncode}; inspect host logs')
    return result.stdout


def trusted(path):
    path = path.resolve(strict=True)
    stat = path.stat()
    if stat.st_uid != 0 or stat.st_mode & 0o022:
        raise RuntimeError('Release artifact is not root-owned and protected')
    return path


def compose(path, *args):
    return ['docker', 'compose', '-p', PROJECT, '-f', str(path), *args]


def verify_health():
    for _ in range(60):
        try:
            with urlopen('http://127.0.0.1:7001/api/health', timeout=3) as response:
                if response.status == 200:
                    info = json.loads(run(['docker', 'inspect', CONTAINER]))[0]
                    if info.get('State', {}).get('Running'):
                        return
        except Exception:
            pass
        time.sleep(2)
    raise RuntimeError('FacelessForge did not return healthy HTTP 200')


def verify_artifacts(expected):
    if not isinstance(expected, dict) or not expected:
        raise RuntimeError('Release has no verified artifact hashes')
    for path, sha in expected.items():
        if not isinstance(path, str) or not path.startswith(('/app/backend/app/', '/app/deploy/backend/app/', '/app/frontend/build/', '/app/deploy/frontend/build/')) or '..' in path.split('/') or not re.fullmatch('[0-9a-f]{64}', sha):
            raise RuntimeError('Invalid artifact proof')
    code = 'import sys,json,hashlib,pathlib; d=json.load(sys.stdin); bad=[p for p,h in d.items() if not pathlib.Path(p).is_file() or hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()!=h]; print(json.dumps({"mismatches":bad})); sys.exit(bool(bad))'
    result = subprocess.run(['docker', 'exec', '-i', CONTAINER, 'python3', '-c', code], input=json.dumps(expected), text=True, capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError('Live artifacts do not match the verified release')


def main():
    if len(sys.argv) not in (2, 3) or not re.fullmatch('[0-9a-f]{40}', sys.argv[1]) or (len(sys.argv) == 3 and sys.argv[2] != '--check'):
        raise RuntimeError('Supply a full lowercase commit SHA and optional --check')
    sha = sys.argv[1]
    release = trusted(ROOT / 'releases' / sha)
    manifest_path = trusted(release / 'manifest.json')
    manifest = json.loads(manifest_path.read_text())
    if manifest.get('commit') != sha or manifest.get('tests_passed') is not True or manifest.get('frontend_build_passed') is not True:
        raise RuntimeError('Release verification is incomplete')
    candidate = trusted(release / 'compose.yml')
    if hashlib.sha256(candidate.read_bytes()).hexdigest() != manifest.get('compose_sha256'):
        raise RuntimeError('Candidate configuration changed after verification')
    info = json.loads(run(['docker', 'inspect', CONTAINER]))[0]
    labels = info.get('Config', {}).get('Labels') or {}
    if labels.get('com.docker.compose.project') != PROJECT:
        raise RuntimeError('Unexpected production Compose project')
    previous_paths = labels.get('com.docker.compose.project.config_files', '').split(',')
    if len(previous_paths) != 1:
        raise RuntimeError('Prior Compose configuration requires operator reconciliation')
    previous = trusted(Path(previous_paths[0]))
    old = json.loads(run(compose(previous, 'config', '--format', 'json')))
    new = json.loads(run(compose(candidate, 'config', '--format', 'json')))
    old_app, new_app = old.get('services', {}).get('app', {}), new.get('services', {}).get('app', {})
    old_image = old_app.get('image')
    new_image = new_app.get('image')
    if not isinstance(new_image, str) or new_image != manifest.get('candidate_image'):
        raise RuntimeError('Candidate image does not match the verified manifest')
    # The release may change only the app image. Network, mounts, environment,
    # restart policy, database services and all other settings must match.
    new_app['image'] = old_image
    if new != old:
        raise RuntimeError('Candidate changes more than the app image; activation refused')
    image = json.loads(run(['docker', 'image', 'inspect', new_image]))[0]
    if image['Id'] != manifest.get('candidate_image_id') or image.get('Config', {}).get('Labels', {}).get('org.opencontainers.image.revision') != sha:
        raise RuntimeError('Image identity/revision does not match verified candidate')
    if info['Image'] == image['Id']:
        verify_health()
        verify_artifacts(manifest.get('artifact_sha256'))
        print(json.dumps({'status': 'already_deployed', 'target_commit': sha, 'image_id': image['Id']}))
        return
    if info['Image'] != manifest.get('expected_base_image_id'):
        raise RuntimeError('Production moved since release preparation; reconcile before activation')
    run(['python3', str(ROOT / 'guard-idle.py')], timeout=30)
    if len(sys.argv) == 3:
        print(json.dumps({'status': 'check_passed', 'target_commit': sha, 'activation_performed': False}))
        return
    try:
        run(compose(candidate, 'up', '-d', '--no-deps', '--pull', 'never', 'app'))
        verify_health()
        verify_artifacts(manifest.get('artifact_sha256'))
    except Exception:
        try:
            run(compose(previous, 'up', '-d', '--no-deps', '--pull', 'never', 'app'))
            verify_health()
        except Exception:
            raise RuntimeError('Activation failed; rollback needs host intervention') from None
        raise RuntimeError('Activation verification failed; previous app image restored') from None
    receipt = {'status': 'deployed', 'target_commit': sha, 'image_id': image['Id'], 'preserved_runtime_overlay': True, 'verified_artifact_count': len(manifest['artifact_sha256']), 'deployed_at': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'rollback_compose': str(previous)}
    output = ROOT / 'state' / 'last-deployment.json'
    output.write_text(json.dumps(receipt))
    output.chmod(0o600)
    print(json.dumps(receipt))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Messages above intentionally do not include command output/env values.
        print(json.dumps({'status': 'failed', 'error': str(exc) if isinstance(exc, RuntimeError) else 'Deployment validation failed; inspect host'}))
        raise SystemExit(1)
