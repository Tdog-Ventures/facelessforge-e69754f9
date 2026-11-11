import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('prepared', Path(__file__).with_name('deploy-prepared.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PreparedReleaseTest(unittest.TestCase):
    def run_release(self, *, change_environment=False, active=False, artifact_failure=False, check=False):
        sha = 'a' * 40
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            release = root / 'releases' / sha
            release.mkdir(parents=True)
            (root / 'state').mkdir()
            compose = release / 'compose.yml'
            compose.write_text('verified-compose')
            manifest = {'commit': sha, 'tests_passed': True, 'frontend_build_passed': True, 'compose_sha256': hashlib.sha256(compose.read_bytes()).hexdigest(), 'candidate_image': 'ff:candidate', 'candidate_image_id': 'sha256:new', 'expected_base_image_id': 'sha256:old', 'artifact_sha256': {'/app/backend/app/render.py': 'a' * 64}}
            (release / 'manifest.json').write_text(json.dumps(manifest))
            previous = root / 'old-compose.yml'
            info = {'Image': 'sha256:old', 'Config': {'Labels': {'com.docker.compose.project': 'ff-continuity', 'com.docker.compose.project.config_files': str(previous)}}}
            image = {'Id': 'sha256:new', 'Config': {'Labels': {'org.opencontainers.image.revision': sha}}}
            def command(args, timeout=120):
                calls.append(args)
                if args[:2] == ['docker', 'inspect']:
                    return json.dumps([info])
                if args[:3] == ['docker', 'image', 'inspect']:
                    return json.dumps([image])
                if args[-3:] == ['config', '--format', 'json']:
                    candidate = str(compose) in args
                    return json.dumps({'services': {'app': {'image': 'ff:candidate' if candidate else 'ff:old', 'environment': {'SETTING': 'changed' if candidate and change_environment else 'preserved'}}}})
                if args[0] == 'python3' and active:
                    raise RuntimeError('Recorded work is active')
                return ''
            failure = RuntimeError('artifact mismatch') if artifact_failure else None
            with patch.object(module, 'ROOT', root), patch.object(module, 'trusted', side_effect=lambda path: path), patch.object(module, 'run', side_effect=command), patch.object(module, 'verify_health'), patch.object(module, 'verify_artifacts', side_effect=failure), patch('sys.argv', ['deploy-prepared.py', sha] + (['--check'] if check else [])), contextlib.redirect_stdout(io.StringIO()):
                try:
                    module.main()
                    error = None
                except RuntimeError as exc:
                    error = str(exc)
            return calls, error

    def test_check_only_never_activates(self):
        calls, error = self.run_release(check=True)
        self.assertIsNone(error)
        self.assertFalse(any('up' in args for args in calls))

    def test_environment_change_is_refused(self):
        calls, error = self.run_release(change_environment=True)
        self.assertIn('more than the app image', error)
        self.assertFalse(any('up' in args for args in calls))

    def test_active_work_is_refused(self):
        calls, error = self.run_release(active=True)
        self.assertEqual(error, 'Recorded work is active')
        self.assertFalse(any('up' in args for args in calls))

    def test_artifact_failure_rolls_back(self):
        calls, error = self.run_release(artifact_failure=True)
        activation = [args for args in calls if 'up' in args]
        self.assertEqual(len(activation), 2)
        self.assertIn('previous app image restored', error)
        self.assertTrue(any('old-compose.yml' in item for item in activation[1]))


if __name__ == '__main__':
    unittest.main()

