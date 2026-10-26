#!/usr/bin/env python3
"""Build a reproducible single-file installer. No runtime downloads/dependencies."""
import base64
import gzip
import hashlib
import io
from pathlib import Path
import tarfile

base = Path(__file__).parent
raw = io.BytesIO()
with tarfile.open(fileobj=raw, mode='w') as tar:
    for name in ('receiver.py', 'bootstrap.py'):
        data = (base / name).read_bytes()
        info = tarfile.TarInfo(name)
        info.size = len(data)
        info.mode = 0o600
        tar.addfile(info, io.BytesIO(data))
payload = gzip.compress(raw.getvalue(), mtime=0)
encoded = base64.b64encode(payload).decode()
digest = hashlib.sha256(payload).hexdigest()
script = '''#!/usr/bin/env bash
set -euo pipefail
if [ "$(id -u)" != 0 ]; then echo 'Use the Selectel root web console'; exit 1; fi
command -v python3 >/dev/null || { echo 'Python 3 is required'; exit 1; }
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
python3 - "$work" <<'TANEM_PAYLOAD'
import base64,hashlib,io,pathlib,sys,tarfile
payload=base64.b64decode('ENCODED')
assert hashlib.sha256(payload).hexdigest()=='DIGEST'
with tarfile.open(fileobj=io.BytesIO(payload),mode='r:gz') as tar:
    for member in tar:
        assert member.name in ('receiver.py','bootstrap.py') and member.isfile()
        pathlib.Path(sys.argv[1],member.name).write_bytes(tar.extractfile(member).read())
TANEM_PAYLOAD
python3 "$work/bootstrap.py" "$@"
'''.replace('ENCODED', encoded).replace('DIGEST', digest)
(base / 'install.sh').write_text(script)
print('Built deploy/install.sh')
