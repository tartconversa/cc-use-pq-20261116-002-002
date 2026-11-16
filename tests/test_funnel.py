import json

from community_fitness.funnel import Funnel, REASON_LEGEND
from tests.helpers import Scenario, START, END, TEN, ELEVEN


class FunnelTests(Scenario):
    def setUp(self):
        super().setUp()
        self.funnel = Funnel(self.store)

    def _two_district_event(self):
        """One group, three teams across two districts with unequal sizes.

        t1 never checks in; t0 arrives partially; t2 arrives in full. Built
        while the event is still a draft because registration locks on open.
        """
        self.competition.create_event('evt', 'Community showcase', START, END,
                                      'Asia/Shanghai')
        self.competition.add_group('evt', 'dance', 'Dance', {'form': 60, 'rhythm': 40}, 2, 3)
        self.competition.register_team('evt', 't0', 'dance', 'Team 0', 'Qingpu', 3)
        self.competition.register_team('evt', 't1', 'dance', 'Team 1', 'Qingpu', 3)
        self.competition.register_team('evt', 't2', 'dance', 'Team Two', 'Minhang', 5)
        for number in range(2):
            self.competition.assign_judge('evt', f'j{number}', f'Judge {number}', 'dance')
        self.services.add_zone('evt', 'tea', 'Tea counter', 'tea',
                               '2026-10-26T09:00:00+08:00', '2026-10-26T18:00:00+08:00')
        self.competition.change_state('evt', 'open', self.revision())
        self.sessions.open('evt', 'dance', self.revision())

        # t0: only 2 of 3 members arrive (one member no-show).
        self.attendance.check_in('evt', 't0', 'm1', 'north', TEN, 'c1')
        self.attendance.check_in('evt', 't0', 'm2', 'north', TEN, 'c2')
        # Replayed/retried check-in must not create a second arrival.
        replay = self.attendance.check_in('evt', 't0', 'm2', 'north', TEN, 'c2')
        self.assertTrue(replay['duplicate'])
        # t2: all 5 arrive. t1 never arrives.
        for member in range(5):
            self.attendance.check_in('evt', 't2', f'm{member}', 'south', ELEVEN,
                                     f'c-t2-{member}')

    def test_live_funnel_denominators_and_reasons(self):
        self._two_district_event()
        # Score only t0 and t2 fully; t1 has no cards and never showed up.
        for judge in ('j0', 'j1'):
            self.competition.submit_score('evt', 't0', judge,
                                          {'form': 52, 'rhythm': 34}, f't0-{judge}')
            self.competition.submit_score('evt', 't2', judge,
                                          {'form': 40, 'rhythm': 30}, f't2-{judge}')
        report = self.funnel.live('evt', award_top_n=1)

        self.assertEqual(report['basis'], 'live')
        self.assertIsNone(report['version'])
        self.assertEqual(report['comparison_key'], 'evt:live')
        totals = report['totals']
        # 3 registered teams: t0(3), t1(3), t2(5) members.
        self.assertEqual(totals['registered_teams'], 3)
        self.assertEqual(totals['registered_members'], 11)
        self.assertEqual(totals['attended_teams'], 2)           # t0, t2
        self.assertEqual(totals['arrived_members'], 7)          # 2 + 5, retry deduped
        self.assertEqual(totals['member_no_shows'], 4)          # t0:1, t1:3, t2:0
        # Cards are in but the scoring session is still open, so no result is
        # settled yet: completed_card_teams counts them, scored_teams does not.
        self.assertEqual(totals['completed_card_teams'], 2)
        self.assertEqual(totals['scored_teams'], 0)
        self.assertEqual(totals['awarded_teams'], 0)

        # Every rate names its own denominator.
        self.assertEqual(totals['team_attendance_rate'], '0.667')
        self.assertEqual(totals['member_attendance_rate'], '0.636')
        # Completion base is attended teams (2); nothing is settled yet.
        self.assertEqual(totals['score_completion_rate'], '0.000')
        self.assertEqual(totals['score_completion_rate_of_registered'], '0.000')
        # Zero scored teams means award conversion has no denominator.
        self.assertIsNone(totals['award_conversion_rate'])

        group = report['groups'][0]
        reasons = group['reasons']
        # t1 is absent: excluded from scoring denominator, flagged separately.
        self.assertEqual(reasons.get('not_scored_no_show'), 1)
        # t0 attended with one missing member.
        self.assertEqual(reasons.get('member_no_show'), 1)
        # t0/t2 have full cards but the session stays open.
        self.assertEqual(reasons.get('session_not_closed'), 2)

        by_id = {row['team_id']: row for row in group['teams_detail']}
        self.assertEqual(by_id['t1']['stage'], 'registered')
        self.assertEqual(by_id['t1']['drop_reason'], 'not_scored_no_show')
        self.assertFalse(by_id['t1']['attended'])
        self.assertEqual(by_id['t0']['member_no_shows'], 1)
        self.assertEqual(by_id['t0']['drop_reason'], 'session_not_closed')

        # Close the session (t1 still needs cards for the domain rule); after
        # closing, settled teams enter the scored/awarded stages.
        for judge in ('j0', 'j1'):
            self.competition.submit_score('evt', 't1', judge,
                                          {'form': 40, 'rhythm': 30}, f't1-{judge}')
        self.sessions.close('evt', 'dance', self.revision())
        settled = self.funnel.live('evt', award_top_n=1)
        self.assertEqual(settled['totals']['completed_card_teams'], 3)
        self.assertEqual(settled['totals']['scored_teams'], 2)   # t0, t2 attended
        self.assertEqual(settled['totals']['awarded_teams'], 1)  # group top-1
        self.assertEqual(settled['totals']['score_completion_rate'], '1.000')
        self.assertEqual(settled['totals']['award_conversion_rate'], '0.500')
        settled_group = settled['groups'][0]
        self.assertEqual(settled_group['reasons'].get('rank_out_of_awards'), 1)
        # t1 stays out of the scoring denominator even though it now has cards.
        self.assertEqual(settled_group['reasons'].get('not_scored_no_show'), 1)

        # District roll-ups keep the same denominator semantics.
        qingpu = next(row for row in settled['districts']
                      if row['district'] == 'Qingpu')
        self.assertEqual(qingpu['registered_teams'], 2)   # t0, t1
        self.assertEqual(qingpu['attended_teams'], 1)
        minhang = next(row for row in settled['districts'] if row['district'] == 'Minhang')
        self.assertEqual(minhang['registered_members'], 5)
        self.assertEqual(minhang['arrived_members'], 5)
        self.assertEqual(minhang['member_attendance_rate'], '1.000')

    def test_missing_cards_do_not_depress_completion_for_absent_teams(self):
        self._two_district_event()
        # Only t0 gets a full card set; t2 attended but unscored, t1 absent.
        for judge in ('j0', 'j1'):
            self.competition.submit_score('evt', 't0', judge,
                                          {'form': 52, 'rhythm': 34}, f't0-{judge}')
        # Close the session is impossible while t2 lacks cards, so reason
        # priority is observed on the live view: t0 is session_not_closed,
        # t2 is missing_scores, t1 is not_scored_no_show.
        report = self.funnel.live('evt')
        group = report['groups'][0]
        # Denominator is the 2 attended teams; t1's absence is not "poor scoring".
        self.assertEqual(group['attended_teams'], 2)
        self.assertEqual(group['score_completion_rate'], '0.000')
        self.assertEqual(group['reasons'].get('not_scored_no_show'), 1)
        self.assertEqual(group['reasons'].get('missing_scores'), 1)
        self.assertEqual(group['reasons'].get('session_not_closed'), 1)
        details = {row['team_id']: row for row in group['teams_detail']}
        self.assertEqual(details['t2']['drop_reason'], 'missing_scores')
        self.assertIn('j0', details['t2']['drop_detail'])
        self.assertEqual(details['t0']['drop_reason'], 'session_not_closed')
        # completed_card_teams still reflects t0's full card set.
        self.assertEqual(group['completed_card_teams'], 1)

    def test_open_dispute_blocks_settlement(self):
        self._two_district_event()
        for judge in ('j0', 'j1'):
            self.competition.submit_score('evt', 't0', judge,
                                          {'form': 52, 'rhythm': 34}, f't0-{judge}')
            self.competition.submit_score('evt', 't2', judge,
                                          {'form': 40, 'rhythm': 30}, f't2-{judge}')
        # An open dispute on t0 keeps its result unsettled even with full cards.
        self.disputes.open('evt', 'd1', 't0', 'j0', '分数存疑')
        report = self.funnel.live('evt')
        details = {row['team_id']: row for row in report['groups'][0]['teams_detail']}
        self.assertTrue(details['t0']['complete'])
        self.assertFalse(details['t0']['finalized'])
        self.assertEqual(details['t0']['drop_reason'], 'open_dispute')
        self.assertEqual(report['groups'][0]['reasons'].get('open_dispute'), 1)
        self.assertEqual(report['totals']['scored_teams'], 0)
        # Resolving the dispute alone does not settle it: the session is open.
        self.disputes.resolve('evt', 'd1', 'rejected', '复核维持原判')
        report = self.funnel.live('evt')
        details = {row['team_id']: row for row in report['groups'][0]['teams_detail']}
        self.assertEqual(details['t0']['drop_reason'], 'session_not_closed')

    def test_insufficient_judges_reason(self):
        # Build a fresh event where the group never meets min_judges. The team
        # does check in, so the eligibility failure (not absence) is the reason.
        self.competition.create_event('evt2', 'Thin panel', '2026-10-26T08:00:00+08:00',
                                      '2026-10-26T19:00:00+08:00', 'Asia/Shanghai')
        self.competition.add_group('evt2', 'g', 'G', {'a': 60, 'b': 40}, 2, 3)
        self.competition.register_team('evt2', 'x', 'g', 'X', 'Qingpu', 2)
        self.competition.assign_judge('evt2', 'sole', 'Sole judge', 'g')
        self.competition.change_state('evt2', 'open',
                                      self.store.inspect('evt2')['revision'])
        self.attendance.check_in('evt2', 'x', 'm1', 'north', TEN, 'ck')
        report = self.funnel.live('evt2')
        team = report['groups'][0]['teams_detail'][0]
        self.assertFalse(team['complete'])
        self.assertEqual(team['drop_reason'], 'insufficient_judges')
        # One attended team, zero complete: an explicit zero, not a blank rate.
        self.assertEqual(report['groups'][0]['score_completion_rate'], '0.000')

    def test_snapshot_funnel_is_versioned_and_immune_to_late_checkins(self):
        self._two_district_event()
        # Every team needs a full card set to close the session, including t1
        # which never checked in; the funnel still keeps t1 out of the scoring
        # denominator via not_scored_no_show.
        for team in ('t0', 't1', 't2'):
            for judge in ('j0', 'j1'):
                self.competition.submit_score('evt', team, judge,
                                              {'form': 52, 'rhythm': 34}, f'{team}-{judge}')
        self.close()
        snapshot = self.publish()
        version = snapshot['version']

        frozen = self.funnel.snapshot('evt', version)
        self.assertEqual(frozen['basis'], 'snapshot')
        self.assertEqual(frozen['version'], version)
        self.assertEqual(frozen['source_revision'], snapshot['event']['revision'])
        self.assertEqual(frozen['comparison_key'], f'evt:snapshot:v{version}')
        self.assertEqual(frozen['totals']['arrived_members'], 7)

        # Late back-fill after publication: live view changes, old snapshot does not.
        self.attendance.check_in('evt', 't0', 'm3', 'north', TEN, 'late-1')
        live = self.funnel.live('evt')
        self.assertEqual(live['totals']['arrived_members'], 8)
        again = self.funnel.snapshot('evt', version)
        self.assertEqual(again['totals']['arrived_members'], 7)
        self.assertNotEqual(live['comparison_key'], again['comparison_key'])

        # Head snapshot resolves without an explicit version.
        head = self.funnel.snapshot('evt', None)
        self.assertEqual(head['version'], version)

    def test_snapshot_missing_version_raises(self):
        self.seed()
        with self.assertRaises(Exception) as ctx:
            self.funnel.snapshot('evt', 9)
        self.assertEqual(ctx.exception.code, 'snapshot_not_found')

    def test_csv_rows_carry_basis_and_all_reason_columns(self):
        self._two_district_event()
        for team in ('t0', 't1', 't2'):
            for judge in ('j0', 'j1'):
                self.competition.submit_score('evt', team, judge,
                                              {'form': 52, 'rhythm': 34}, f'{team}-{judge}')
        self.close()
        self.publish()
        report = self.funnel.snapshot('evt', 1)
        text = self.funnel.csv(report)
        lines = text.strip().splitlines()
        header = lines[0].split(',')
        self.assertIn('basis', header)
        for code in REASON_LEGEND:
            self.assertIn(f'reason_{code}', header)
        # TOTAL + two district rows + one group row.
        self.assertEqual(len(lines), 1 + 1 + 2 + 1)
        total_cells = lines[1].split(',')
        self.assertEqual(total_cells[header.index('basis')], 'snapshot')
        self.assertEqual(total_cells[header.index('version')], '1')

    def test_invalid_top_n(self):
        self.seed()
        with self.assertRaises(Exception) as ctx:
            self.funnel.live('evt', award_top_n=0)
        self.assertEqual(ctx.exception.code, 'invalid_top_n')

    def test_stage_definitions_pin_denominators(self):
        self.seed()
        report = self.funnel.live('evt')
        stages = {item['stage']: item for item in report['stage_definitions']}
        self.assertIn('到场队伍数', stages['scored']['denominator'])
        self.assertIn('完成评分队伍数', stages['awarded']['denominator'])
        self.assertIn('去重', stages['attended']['deduplication'])
        # Every reason code used in rows is explained by the legend.
        legend = set(report['reason_legend'])
        for group in report['groups']:
            self.assertTrue(set(group['reasons']) <= legend)
