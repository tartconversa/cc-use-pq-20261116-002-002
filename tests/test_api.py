import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from community_fitness.api import create_app
from tests.helpers import Scenario, START, END, TEN


class ApiTests(Scenario):
    def setUp(self):
        super().setUp()
        self.client = TestClient(create_app(Path(self.temp.name) / 'api.sqlite3'))

    def test_create_and_get_event(self):
        body = {'id': 'api1', 'name': 'Open day', 'starts_at': START,
                'ends_at': END, 'timezone': 'Asia/Shanghai'}
        response = self.client.post('/events', json=body)
        self.assertEqual(response.status_code, 201)
        loaded = self.client.get('/events/api1')
        self.assertEqual(loaded.json()['name'], 'Open day')

    def test_error_payload(self):
        response = self.client.get('/events/not-there')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()['error'], 'not_found')

    def test_api_full_scoring_and_export(self):
        self.client.post('/events', json={'id': 'api1', 'name': 'Open day',
                                         'starts_at': START, 'ends_at': END,
                                         'timezone': 'Asia/Shanghai'})
        group = {'code': 'dance', 'label': 'Dance', 'criteria': {'form': 60, 'music': 40},
                 'min_judges': 2, 'max_judges': 2}
        self.assertEqual(self.client.post('/events/api1/groups', json=group).status_code, 201)
        team = {'id': 't1', 'group_code': 'dance', 'name': 'Local team',
                'district': 'Qingpu', 'member_count': 2}
        self.assertEqual(self.client.post('/events/api1/teams', json=team).status_code, 201)
        for judge in ('j1', 'j2'):
            body = {'id': judge, 'name': judge, 'group_code': 'dance'}
            self.assertEqual(self.client.post('/events/api1/judges', json=body).status_code, 201)
        revision = self.client.get('/events/api1').json()['revision']
        self.client.post('/events/api1/state', json={'target': 'open',
                                                     'expected_revision': revision})
        revision = self.client.get('/events/api1').json()['revision']
        self.client.post('/events/api1/groups/dance/session',
                         json={'expected_event_revision': revision})
        for judge in ('j1', 'j2'):
            response = self.client.post('/events/api1/scores', json={
                'team_id': 't1', 'judge_id': judge,
                'scores': {'form': 55, 'music': 35}, 'request_key': judge})
            self.assertEqual(response.status_code, 200)
        revision = self.client.get('/events/api1').json()['revision']
        self.client.post('/events/api1/groups/dance/session/close',
                         json={'expected_event_revision': revision})
        revision = self.client.get('/events/api1').json()['revision']
        self.client.post('/events/api1/state', json={'target': 'closed',
                                                     'expected_revision': revision})
        preview = self.client.get('/events/api1/preview').json()
        published = self.client.post('/events/api1/publish', json={
            'expected_head': 0, 'expected_revision': preview['event']['revision']})
        self.assertEqual(published.status_code, 200)
        self.assertEqual(published.json()['scores']['groups'][0]['teams'][0]['score'], '90.00')
        exported = self.client.get('/events/api1/export/standings.csv')
        self.assertEqual(exported.status_code, 200)
        self.assertIn('Local team', exported.text)

    def test_api_validation_error(self):
        response = self.client.post('/events', json={'id': 'x'})
        self.assertEqual(response.status_code, 422)

    def test_api_funnel_live_and_snapshot_are_separate_views(self):
        self.client.post('/events', json={'id': 'api2', 'name': 'Open day',
                                         'starts_at': START, 'ends_at': END,
                                         'timezone': 'Asia/Shanghai'})
        group = {'code': 'dance', 'label': 'Dance', 'criteria': {'form': 60, 'music': 40},
                 'min_judges': 1, 'max_judges': 2}
        self.client.post('/events/api2/groups', json=group)
        team = {'id': 't1', 'group_code': 'dance', 'name': 'Local team',
                'district': 'Qingpu', 'member_count': 2}
        self.client.post('/events/api2/teams', json=team)
        self.client.post('/events/api2/judges',
                         json={'id': 'j1', 'name': 'j1', 'group_code': 'dance'})
        revision = self.client.get('/events/api2').json()['revision']
        self.client.post('/events/api2/state',
                         json={'target': 'open', 'expected_revision': revision})
        revision = self.client.get('/events/api2').json()['revision']
        self.client.post('/events/api2/groups/dance/session',
                         json={'expected_event_revision': revision})
        checkin = {'team_id': 't1', 'member_ref': 'm1', 'gate': 'north',
                   'arrived_at': TEN, 'request_key': 'ck1'}
        self.assertEqual(self.client.post('/events/api2/attendance', json=checkin).status_code, 201)
        self.client.post('/events/api2/scores', json={
            'team_id': 't1', 'judge_id': 'j1',
            'scores': {'form': 55, 'music': 35}, 'request_key': 's1'})

        live = self.client.get('/events/api2/funnel/live').json()
        self.assertEqual(live['basis'], 'live')
        self.assertIsNone(live['version'])
        row = live['groups'][0]
        self.assertEqual(row['registered_teams'], 1)
        self.assertEqual(row['attended_teams'], 1)
        self.assertEqual(row['arrived_members'], 1)
        self.assertEqual(row['member_no_shows'], 1)
        # Session still open: cards complete but the result is not settled.
        self.assertEqual(row['completed_card_teams'], 1)
        self.assertEqual(row['scored_teams'], 0)
        self.assertEqual(row['reasons']['session_not_closed'], 1)
        # Snapshot view must not exist before publication.
        self.assertEqual(self.client.get('/events/api2/funnel/snapshot').status_code, 404)

        revision = self.client.get('/events/api2').json()['revision']
        self.client.post('/events/api2/groups/dance/session/close',
                         json={'expected_event_revision': revision})
        revision = self.client.get('/events/api2').json()['revision']
        self.client.post('/events/api2/state',
                         json={'target': 'closed', 'expected_revision': revision})
        preview = self.client.get('/events/api2/preview').json()
        self.client.post('/events/api2/publish',
                         json={'expected_head': 0,
                               'expected_revision': preview['event']['revision']})
        snap = self.client.get('/events/api2/funnel/snapshot').json()
        self.assertEqual(snap['basis'], 'snapshot')
        self.assertEqual(snap['version'], 1)
        # The frozen snapshot reflects the settled result; the live totals did
        # not change because no further writes happened.
        self.assertEqual(snap['totals']['scored_teams'], 1)
        self.assertEqual(snap['totals']['registered_teams'],
                         live['totals']['registered_teams'])
        self.assertEqual(snap['totals']['arrived_members'],
                         live['totals']['arrived_members'])
        self.assertNotEqual(snap['comparison_key'], live['comparison_key'])

        exported = self.client.get('/events/api2/export/funnel.csv')
        self.assertEqual(exported.status_code, 200)
        self.assertIn('snapshot', exported.text)
        bad_basis = self.client.get('/events/api2/export/funnel.csv?basis=stale')
        self.assertEqual(bad_basis.status_code, 400)
        self.assertEqual(bad_basis.json()['error'], 'invalid_basis')

    def test_api_health(self):
        self.assertEqual(self.client.get('/health').json(), {'status': 'ok'})
