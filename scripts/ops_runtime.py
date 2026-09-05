"""Standard-library-only probes shared by release and stable-bin operations."""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

OPS_VERSION = 'qqbot-ops-2026.09.05.1'
EXPECTED_PORTS = {'carrot': 6199, 'kona': 6299}
HTTP_PORTS = {'carrot': 6201, 'kona': 6301}


def tool_identity(path: Path) -> dict[str, str]:
    return {'ops_version': OPS_VERSION, 'tool_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'runtime_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def read_environment(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.removeprefix('export ').split('=', 1)
        value = value.strip()
        if value.startswith(('"', "'")):
            quoted = re.match(r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*' ''', value, re.X)
            if quoted is None or (value[quoted.end():].strip() and not value[quoted.end():].lstrip().startswith('#')):
                raise ValueError('invalid quoted environment value')
            value = quoted[0][1:-1]
        else:
            value = value.split(' #', 1)[0].rstrip()
        values[key.strip()] = value
    return values


def cgroup_pids(unit: str, run) -> set[int]:
    group = run('systemctl', 'show', unit, '-p', 'ControlGroup', '--value').strip()
    if not group.startswith('/') or group == '/' or '..' in Path(group).parts:
        raise RuntimeError('service has no dedicated cgroup')
    path = Path('/sys/fs/cgroup') / group.lstrip('/')
    return {int(line) for file in path.rglob('cgroup.procs')
            for line in file.read_text().splitlines() if line.isdigit()}


def _endpoint(value: str) -> tuple[str, int] | None:
    try:
        host, port = value.rsplit(':', 1)
        address = ipaddress.ip_address(host.strip('[]'))
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return str(address), int(port)
    except (ValueError, TypeError):
        return None


def exact_onebot_sockets(output: str, port: int, bot_pids: set[int], napcat_pids: set[int]) -> bool:
    """Require bot-owned LISTEN and both cgroup-owned ends of one loopback TCP pair."""
    target = ('127.0.0.1', port)
    listening = False
    bot_connections, napcat_connections = set(), set()
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 5:
            continue
        state, local, peer = fields[0], _endpoint(fields[3]), _endpoint(fields[4])
        owners = {int(pid) for pid in re.findall(r'\bpid=(\d+)\b', line)}
        if local == target and state == 'LISTEN' and owners & bot_pids:
            listening = True
        if state != 'ESTAB' or local is None or peer is None:
            continue
        if local == target and peer[0] == '127.0.0.1' and owners & bot_pids:
            bot_connections.add((local, peer))
        if peer == target and local[0] == '127.0.0.1' and owners & napcat_pids:
            napcat_connections.add((peer, local))
    return listening and bool(bot_connections & napcat_connections)


def onebot_status(instance: str, values: dict[str, str], *, connection_factory=None) -> dict[str, object]:
    expected = values.get('BOT_SELF_ID', '')
    token = values.get('NAPCAT_ACCESS_TOKEN', '')
    if not re.fullmatch(r'[0-9]{5,12}', expected) or not token:
        raise RuntimeError('expected BOT_SELF_ID or OneBot token is missing')
    url = urlsplit(values.get('ONEBOT_HTTP_URL') or f'http://127.0.0.1:{HTTP_PORTS[instance]}')
    if (url.scheme != 'http' or url.hostname != '127.0.0.1' or url.username or url.password
            or url.path not in ('', '/') or url.query or url.fragment):
        raise RuntimeError('OneBot HTTP endpoint must be a plain IPv4 loopback origin')
    try:
        port = url.port
    except ValueError as exc:
        raise RuntimeError('invalid OneBot HTTP port') from exc
    if port is None or not 1 <= port <= 65535:
        raise RuntimeError('OneBot HTTP endpoint requires an explicit port')
    factory = connection_factory or http.client.HTTPConnection
    results = {}
    for action in ('get_login_info', 'get_status'):
        connection = factory('127.0.0.1', port, timeout=3)
        try:
            connection.request('POST', '/'+action, body=b'{}', headers={
                'Authorization': 'Bearer '+token, 'Content-Type': 'application/json',
            })
            response = connection.getresponse()
            raw = response.read(65537)
            if response.status != 200 or len(raw) > 65536:
                raise RuntimeError('OneBot read-only API returned an invalid response')
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get('status') != 'ok' or data.get('retcode') != 0:
                raise RuntimeError('OneBot read-only API rejected the probe')
            results[action] = data['data']
        except Exception as exc:
            # Exception messages/response bodies may contain credentials or user information.
            raise RuntimeError(f'OneBot {action} probe failed ({type(exc).__name__})') from None
        finally:
            connection.close()
    login, status = results['get_login_info'], results['get_status']
    if not isinstance(login, dict) or str(login.get('user_id', '')) != expected:
        raise RuntimeError('OneBot identity does not match BOT_SELF_ID')
    if not isinstance(status, dict) or status.get('online') is not True or status.get('good') is not True:
        raise RuntimeError('OneBot is not online and healthy')
    return {'identity_matches': True, 'online': True, 'good': True}
