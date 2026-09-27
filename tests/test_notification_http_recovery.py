"""Real socket reconnection and durable history recovery, without a camera or model."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import httpx


def test_http_stream_reconnect_and_disconnect_cleanup(tmp_path):
    root = Path(__file__).resolve().parents[1]
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    environment = {**os.environ, 'TOPOSYNC_DATA_DIR': str(tmp_path / 'data'),
                   'TOPOSYNC_AUTH_MODE': 'bypass', 'TOPOSYNC_NO_FRONTEND': '1'}
    with (tmp_path / 'server.log').open('w') as log:
        process = subprocess.Popen(
            [sys.executable, str(root / 'tests/fixtures/notification_stream_probe.py'), str(port)],
            cwd=root, env=environment, stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            with httpx.Client(base_url=f'http://127.0.0.1:{port}', trust_env=False, timeout=5) as client:
                deadline = time.monotonic() + 15
                while True:
                    assert process.poll() is None, (tmp_path / 'server.log').read_text()
                    try:
                        if client.get('/api/health').status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    assert time.monotonic() < deadline
                    time.sleep(0.05)

                def ready(lines):
                    for line in lines:
                        if line == 'event: ready':
                            return
                    raise AssertionError('stream ended before ready')

                def disconnected():
                    deadline = time.monotonic() + 3
                    while client.get('/__test/subscriptions').json()['count']:
                        assert time.monotonic() < deadline, 'subscription leaked after disconnect'
                        time.sleep(0.02)

                with client.stream('GET', '/api/notifications/stream') as response:
                    assert response.status_code == 200
                    lines = response.iter_lines()
                    ready(lines)
                    first = client.post('/__test/publish').json()[0]
                    received = False
                    for line in lines:
                        if line.startswith('data: '):
                            event = json.loads(line[6:])
                            if event.get('op') == 'insert':
                                assert event['notification']['id'] == first
                                received = True
                                break
                    assert received
                disconnected()
                missed = client.post('/__test/publish?count=2').json()
                with client.stream('GET', '/api/notifications/stream') as response:
                    lines = response.iter_lines()
                    ready(lines)
                    records = client.get('/api/notifications').json()['notifications']
                    assert {record['id'] for record in records} == {first, *missed}
                    assert client.get('/api/notifications/count').json()['total'] == 3
                    assert client.get('/__test/subscriptions').json()['count'] == 1
                disconnected()
                with client.stream('GET', '/api/notifications/stream') as response:
                    lines = response.iter_lines()
                    ready(lines)
                    # Pause socket consumption while publishing at most 12.5 MiB.
                    burst = client.post('/__test/publish?count=400&padding=32768', timeout=15).json()
                    resync = False
                    latest = False
                    for line in lines:
                        if not line.startswith('data: '):
                            continue
                        event = json.loads(line[6:])
                        resync |= event.get('op') == 'resync'
                        if event.get('notification', {}).get('id') == burst[-1]:
                            latest = True
                            break
                    assert resync and latest
                    assert client.get('/api/notifications/count').json()['total'] == 403
                disconnected()
                (tmp_path / 'result.json').write_text(json.dumps({
                    'http_sse_received': True, 'history_recovered': 3,
                    'missed_during_disconnect': 2, 'subscriptions_after_close': 0,
                    'network_backpressure_evaluated': True, 'burst_records': 400,
                    'overflow_resync_received': True, 'latest_received': True,
                }, indent=2))
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
