"""Loopback-only test app with real notification HTTP/SSE routes and no extensions."""
import sys
import uuid
import uvicorn
from fastapi import Query
from toposync.app import create_app
import toposync.extensions.manager as manager

manager._iter_entry_points = lambda _: []
app = create_app()


@app.post('/__test/publish')
async def publish(count: int = Query(default=1, ge=1, le=400), padding: int = Query(default=0, ge=0, le=32768)):
    identifiers = []
    for _ in range(count):
        record = await app.state.notifications.upsert(
            type='test.notification', title='Synthetic stream evidence', description='x' * padding,
            payload={'priority': 'high'}, dedupe_key=uuid.uuid4().hex,
        )
        identifiers.append(record['id'])
    return identifiers


@app.get('/__test/subscriptions')
async def subscriptions():
    return {'count': len(app.state.notifications.broadcaster._subscribers)}


if __name__ == '__main__':
    uvicorn.run(app, host='127.0.0.1', port=int(sys.argv[1]), log_level='warning',
                access_log=False, timeout_graceful_shutdown=2)
