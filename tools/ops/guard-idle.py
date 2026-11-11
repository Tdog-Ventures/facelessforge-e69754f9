"""Fail closed while FacelessForge has recorded active render/provider work.

Reads Mongo through the existing app environment; never emits its credentials.
Does not lock admission: callers must check again immediately before switching.
"""
import json
import subprocess
import sys

CODE = r'''
import json, os, pymongo
client = pymongo.MongoClient(os.environ['MONGODB_URI'], serverSelectionTimeoutMS=6000)
db = client.get_default_database()
active = {
 'render_jobs': ['queued','validating','preparing_assets','rendering','verifying'],
 'voiceover_jobs': ['queued','running'],
 'thumbnail_jobs': ['queued','running'],
 'auto_attach_operations': ['running'],
 'provider_attempts': ['started'],
 'voiceover_attempts': ['queued','running','started'],
}
counts = {}
for collection, states in active.items():
 values = sorted(set(states + [s.upper() for s in states]))
 counts[collection] = db[collection].count_documents({'status': {'$in': values}})
print(json.dumps({'active_counts': counts, 'total': sum(counts.values()),
 'scope':'Recorded render, background voiceover/thumbnail, attachment and provider jobs; not an admission lock.'}))
'''


def main():
    if sys.argv[1:] not in ([], ['--report']):
        print('Invalid arguments', file=sys.stderr)
        return 2
    try:
        result = subprocess.run(['docker', 'exec', '-i', 'facelessforge-app', 'python3', '-c', CODE], capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise RuntimeError()
        report = json.loads(result.stdout)
        if not isinstance(report.get('total'), int):
            raise RuntimeError()
    except Exception:
        print('Active-job verification failed; mutation refused.', file=sys.stderr)
        return 1
    print(json.dumps(report))
    return 0 if '--report' in sys.argv or report['total'] == 0 else 3


if __name__ == '__main__':
    raise SystemExit(main())
