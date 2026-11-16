"""Registration -> attendance -> scoring -> award conversion funnel.

The funnel is computed either from live tables (a mutable, explicitly labelled
view) or from a published snapshot payload (an immutable version). Every row
carries its own denominator and a machine-readable reason code for every
team that drops out, so callers never have to divide raw counts across stages
that use different bases. Live check-in numbers and frozen snapshot standings
must never be joined into one table; the ``basis``/``comparison_key`` fields
make the boundary explicit.
"""

import csv
import io
import json
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP

from .analytics import Analytics
from .errors import DomainError, require
from .storage import Store


# Reason codes explain why a team is missing from the next stage's numerator.
REASON_LEGEND = {
    'member_no_show': '队伍已到场，但部分报名成员未签到（成员口径缺报）',
    'missing_scores': '已到场，但缺少部分在编有效裁判的评分卡',
    'unexpected_scores': '已到场，但存在非本组在编裁判提交的评分卡',
    'insufficient_judges': '组别在编有效裁判数低于 min_judges，评分资格不成立',
    'session_not_closed': '组别评分会话尚未关闭，评分仍可能变化',
    'open_dispute': '队伍相关评分存在未决争议，结果尚未定案',
    'not_scored_no_show': '队伍未到场，不进入完成评分分母，单列而非判为表现差',
    'rank_out_of_awards': '已完成评分，但组内名次超出获奖名额（并列共享名次）',
}

RATE_PLACES = Decimal('0.001')


def _rate(numerator: int, denominator: int) -> str | None:
    """Return a three-decimal rate, or None when the denominator is zero.

    Returning None (rather than 0) keeps an empty denominator visually distinct
    from a real zero conversion, which would otherwise read as poor performance.
    """
    if denominator == 0:
        return None
    value = Decimal(numerator) / Decimal(denominator)
    return str(value.quantize(RATE_PLACES, rounding=ROUND_HALF_UP))


class Funnel:
    def __init__(self, store: Store):
        self.store = store
        self.analytics = Analytics(store)

    # ------------------------------------------------------------------ live
    def live(self, event_id: str, award_top_n: int = 3) -> dict:
        self._validate_top_n(award_top_n)
        with self.store.connection() as db:
            event = self.store.one(
                db, 'SELECT id,revision,state FROM events WHERE id=?', (event_id,))
            groups = self.store.many(
                db, 'SELECT code,label,min_judges FROM groups WHERE event_id=? ORDER BY code',
                (event_id,))
            team_rows = self.store.many(
                db, 'SELECT id,group_code,name,district,member_count FROM teams '
                   'WHERE event_id=? ORDER BY id', (event_id,))
            arrivals = self.store.many(
                db, 'SELECT team_id,member_ref FROM attendance WHERE event_id=?', (event_id,))
            judges = self.store.many(
                db, 'SELECT id,group_code FROM judges WHERE event_id=? AND active=1', (event_id,))
            cards = self.store.many(
                db, 'SELECT team_id,judge_id FROM score_cards WHERE event_id=?', (event_id,))
            sessions = {row['group_code']: row['state'] for row in self.store.many(
                db, 'SELECT group_code,state FROM score_sessions WHERE event_id=?', (event_id,))}
            open_disputes = {row['team_id'] for row in self.store.many(
                db, 'SELECT DISTINCT team_id FROM score_disputes WHERE event_id=? AND state=?',
                (event_id, 'open'))}

        # Attendance counts distinct members: the attendance primary key
        # (event_id, team_id, member_ref) makes retried/replayed check-in
        # requests collapse onto one row, so retries can never inflate arrivals.
        arrived_by_team: dict[str, set[str]] = defaultdict(set)
        for row in arrivals:
            arrived_by_team[row['team_id']].add(row['member_ref'])

        judges_by_group: dict[str, set[str]] = defaultdict(set)
        for judge in judges:
            judges_by_group[judge['group_code']].add(judge['id'])
        cards_by_team: dict[str, set[str]] = defaultdict(set)
        for card in cards:
            cards_by_team[card['team_id']].add(card['judge_id'])

        normalized_groups = []
        for group in groups:
            code = group['code']
            assigned = judges_by_group[code]
            eligible = len(assigned) >= group['min_judges']
            teams = []
            for row in team_rows:
                if row['group_code'] != code:
                    continue
                arrived = len(arrived_by_team.get(row['id'], set()))
                submitted = cards_by_team.get(row['id'], set())
                missing_judges = sorted(assigned - submitted)
                unexpected_judges = sorted(submitted - assigned)
                cards_complete = eligible and not missing_judges and not unexpected_judges
                # "Scored" means the result is settled: the team showed up,
                # cards are complete, no open dispute, and the group session is
                # closed. A full card set inside an open session can still be
                # revised; an absent team never enters the scoring denominator.
                finalized = (arrived > 0 and cards_complete
                             and row['id'] not in open_disputes
                             and sessions.get(code) == 'closed')
                teams.append({
                    'team_id': row['id'], 'name': row['name'], 'district': row['district'],
                    'member_count': row['member_count'], 'arrived_members': arrived,
                    'attended': arrived > 0,
                    'missing_judges': missing_judges, 'unexpected_judges': unexpected_judges,
                    'complete': cards_complete, 'finalized': finalized,
                    'rank': None,  # filled by _rank_live_groups for complete teams
                    'session_state': sessions.get(code),
                    'has_open_dispute': row['id'] in open_disputes,
                })
            normalized_groups.append({'code': code, 'label': group['label'],
                                      'min_judges': group['min_judges'],
                                      'assigned_judges': sorted(assigned),
                                      'eligible': eligible, 'teams': teams})
        return self._build(event_id, basis='live', version=None,
                           source_revision=event['revision'], event_state=event['state'],
                           award_top_n=award_top_n, groups=normalized_groups)

    # -------------------------------------------------------------- snapshot
    def snapshot(self, event_id: str, version: int | None, award_top_n: int = 3) -> dict:
        self._validate_top_n(award_top_n)
        with self.store.connection() as db:
            self.store.one(db, 'SELECT id FROM events WHERE id=?', (event_id,))
            if version is None:
                row = db.execute(
                    'SELECT version,source_revision,payload FROM snapshots '
                    "WHERE event_id=? AND status='published' ORDER BY version DESC LIMIT 1",
                    (event_id,)).fetchone()
            else:
                require(type(version) is int and version > 0, 'invalid_version',
                        'Version must be positive')
                row = db.execute(
                    'SELECT version,source_revision,payload FROM snapshots '
                    "WHERE event_id=? AND version=? AND status='published'",
                    (event_id, version)).fetchone()
            if row is None:
                raise DomainError('snapshot_not_found', 'Published snapshot does not exist', 404)
            payload = json.loads(row['payload'])

        # Attendance inside a snapshot was frozen at publish time, so late
        # back-filled check-ins never alter an old version's funnel.
        arrived_by_team = {item['team_id']: item['arrived']
                           for item in payload['attendance']['teams']}
        normalized_groups = []
        for group in payload['scores']['groups']:
            teams = []
            for entry in group['teams']:
                arrived = arrived_by_team.get(entry['team_id'], 0)
                teams.append({
                    'team_id': entry['team_id'], 'name': entry['team_name'],
                    'district': entry['district'],
                    'member_count': entry['member_count'],
                    'arrived_members': arrived, 'attended': arrived > 0,
                    'missing_judges': entry['missing_judges'],
                    'unexpected_judges': entry['unexpected_judges'],
                    'complete': entry['complete'],
                    # Publication guarantees closed sessions and resolved
                    # disputes, so an attended team with a complete card set is
                    # a settled result; absent teams stay out of this stage.
                    'finalized': entry['complete'] and arrived > 0,
                    'rank': entry['rank'] if entry['complete'] else None,
                    'session_state': 'closed',
                    'has_open_dispute': False,
                })
            normalized_groups.append({'code': group['code'], 'label': group['label'],
                                      'min_judges': group['min_judges'],
                                      'assigned_judges': group['assigned_judges'],
                                      'eligible': group['eligible'], 'teams': teams})
        return self._build(event_id, basis='snapshot', version=row['version'],
                           source_revision=row['source_revision'],
                           event_state=payload['event']['state'],
                           award_top_n=award_top_n, groups=normalized_groups,
                           published_at=payload.get('published_at'))

    # ------------------------------------------------------------- shared build
    def _build(self, event_id: str, *, basis: str, version: int | None,
               source_revision: int | None, event_state: str | None,
               award_top_n: int, groups: list[dict],
               published_at: str | None = None) -> dict:
        if basis == 'live':
            self._rank_live_groups(event_id, groups)

        district_rows: dict[str, dict] = defaultdict(self._new_scope)
        group_payloads = []
        total = self._new_scope()

        for group in groups:
            scope = self._build_group(group, award_top_n)
            group_payloads.append(scope)
            self._accumulate(total, scope)
            # District roll-ups aggregate each team by its own district, so a
            # group spanning districts is never booked to a synthetic bucket.
            for record in scope['teams_detail']:
                self._count_team(district_rows[record['district']], record,
                                 record['drop_reason'])

        districts = []
        for name in sorted(district_rows):
            row = district_rows[name]
            row['district'] = name
            self._finalize_rates(row)
            districts.append(row)
        self._finalize_rates(total)

        comparison_key = (f'{event_id}:{basis}:v{version}' if basis == 'snapshot'
                          else f'{event_id}:{basis}')
        return {
            'event_id': event_id,
            'basis': basis,
            'version': version,
            'source_revision': source_revision,
            'event_state': event_state,
            'published_at': published_at,
            'comparison_key': comparison_key,
            'award_top_n': award_top_n,
            'stage_definitions': self._stage_definitions(),
            'reason_legend': REASON_LEGEND,
            'totals': total,
            'districts': districts,
            'groups': group_payloads,
        }

    def _rank_live_groups(self, event_id: str, groups: list[dict]) -> None:
        """Attach score-based ranks using Analytics' authoritative ranking."""
        analytics_groups = self.analytics.score_preview(event_id)['groups']
        ranks = {entry['team_id']: entry['rank']
                 for analytics_group in analytics_groups
                 for entry in analytics_group['teams'] if entry['complete']}
        for group in groups:
            for team in group['teams']:
                if team['complete']:
                    team['rank'] = ranks.get(team['team_id'])

    def _build_group(self, group: dict, award_top_n: int) -> dict:
        # Awards use the frozen/live ranking per group; teams tied on score
        # share one rank, so every team at rank <= top_n wins. Only settled
        # results (finalized) can be awarded.
        awarded_ids = {team['team_id'] for team in group['teams']
                       if team['finalized'] and team['rank'] is not None
                       and team['rank'] <= award_top_n}
        scope = self._new_scope()
        scope['group_code'] = group['code']
        scope['group_label'] = group['label']
        scope['eligible'] = group['eligible']
        scope['assigned_judges'] = group['assigned_judges']
        scope['min_judges'] = group['min_judges']
        scope['session_state'] = next(
            (team['session_state'] for team in group['teams'] if team['session_state']), None)
        team_records = []
        for team in sorted(group['teams'], key=lambda item: item['team_id']):
            record, reason = self._team_record(group, team, awarded_ids, award_top_n)
            team_records.append(record)
            self._count_team(scope, record, reason)
        scope['teams_detail'] = team_records
        self._finalize_rates(scope)
        return scope

    def _team_record(self, group: dict, team: dict, awarded_ids: set[str],
                     award_top_n: int) -> tuple[dict, str | None]:
        member_missing = team['member_count'] - team['arrived_members']
        # Rank is only meaningful for settled (scored) teams; Analytics ranks
        # every complete card set, including absent teams, so null it out here.
        effective_rank = team['rank'] if team['finalized'] else None
        record = {
            'team_id': team['team_id'], 'name': team['name'], 'district': team['district'],
            'member_count': team['member_count'], 'arrived_members': team['arrived_members'],
            'member_no_shows': member_missing,
            'attended': team['attended'], 'complete': team['complete'],
            'finalized': team['finalized'],
            'rank': effective_rank, 'awarded': team['team_id'] in awarded_ids,
            'stage': None, 'drop_reason': None, 'drop_detail': None,
        }
        if not team['attended']:
            record['stage'] = 'registered'
            record['drop_reason'] = 'not_scored_no_show'
            record['drop_detail'] = '无签到成员；保留在报名分母，剔除出完成评分分母'
            return record, record['drop_reason']
        if not team['finalized']:
            record['stage'] = 'attended'
            reason, detail = self._incomplete_reason(group, team)
            record['drop_reason'] = reason
            record['drop_detail'] = detail
            return record, reason
        if not record['awarded']:
            record['stage'] = 'scored'
            record['drop_reason'] = 'rank_out_of_awards'
            record['drop_detail'] = (f'组内名次 {team["rank"]} 超出获奖名额 top_{award_top_n}'
                                     '（同分并列共享名次）')
            return record, 'rank_out_of_awards'
        record['stage'] = 'awarded'
        return record, None

    def _incomplete_reason(self, group: dict, team: dict) -> tuple[str, str]:
        # Order matters: eligibility first (no valid judging panel at all),
        # then card gaps; once cards are complete, procedural gates explain why
        # the result is not settled yet.
        if not group['eligible']:
            return ('insufficient_judges',
                    f"在编有效裁判 {len(group['assigned_judges'])} 人，"
                    f"低于 min_judges={group['min_judges']}，评分资格不成立")
        if team['missing_judges']:
            return ('missing_scores',
                    f"缺少裁判评分卡：{', '.join(team['missing_judges'])}")
        if team['unexpected_judges']:
            return ('unexpected_scores',
                    f"存在非本组在编裁判评分：{', '.join(team['unexpected_judges'])}")
        if team.get('has_open_dispute'):
            return 'open_dispute', '该队评分卡存在未决争议，结果未定案'
        if team.get('session_state') and team['session_state'] != 'closed':
            return 'session_not_closed', '评分会话仍开启，评分可能继续变化'
        return 'missing_scores', '评分未完成'

    def _count_team(self, scope: dict, record: dict, reason: str | None) -> None:
        scope['registered_teams'] += 1
        scope['registered_members'] += record['member_count']
        if record['attended']:
            scope['attended_teams'] += 1
            scope['arrived_members'] += record['arrived_members']
        scope['member_no_shows'] += record['member_no_shows']
        if record['complete']:
            scope['completed_card_teams'] += 1
        if record['finalized']:
            scope['scored_teams'] += 1
        if record['awarded']:
            scope['awarded_teams'] += 1
        if reason:
            scope['reasons'][reason] += 1
        # Member-level no-show reason applies to attended-but-partial teams too.
        if record['attended'] and record['member_no_shows']:
            scope['reasons']['member_no_show'] += 1

    @staticmethod
    def _new_scope() -> dict:
        return {'registered_teams': 0, 'attended_teams': 0,
                'completed_card_teams': 0, 'scored_teams': 0, 'awarded_teams': 0,
                'registered_members': 0, 'arrived_members': 0,
                'member_no_shows': 0, 'reasons': defaultdict(int)}

    @staticmethod
    def _accumulate(target: dict, source: dict) -> None:
        for key in ('registered_teams', 'attended_teams', 'completed_card_teams',
                    'scored_teams', 'awarded_teams',
                    'registered_members', 'arrived_members', 'member_no_shows'):
            target[key] += source[key]
        for reason, count in source['reasons'].items():
            target['reasons'][reason] += count

    @staticmethod
    def _finalize_rates(scope: dict) -> None:
        scope['team_attendance_rate'] = _rate(scope['attended_teams'],
                                              scope['registered_teams'])
        scope['member_attendance_rate'] = _rate(scope['arrived_members'],
                                                scope['registered_members'])
        # Completion divides by ATTENDED teams: absent teams are reported
        # separately as not_scored_no_show instead of depressing this rate.
        scope['score_completion_rate'] = _rate(scope['scored_teams'],
                                               scope['attended_teams'])
        # Secondary view against the full registration base, labelled so it
        # cannot be confused with the attended-base completion rate.
        scope['score_completion_rate_of_registered'] = _rate(
            scope['scored_teams'], scope['registered_teams'])
        scope['award_conversion_rate'] = _rate(scope['awarded_teams'],
                                               scope['scored_teams'])
        scope['reasons'] = dict(sorted(scope['reasons'].items()))

    @staticmethod
    def _stage_definitions() -> list[dict]:
        return [
            {'stage': 'registered', 'denominator': '报名队伍数 / 报名成员数（teams 表，草稿期锁定）',
             'numerator': '全部报名记录', 'unit': 'team + member'},
            {'stage': 'attended',
             'denominator': '队伍口径=报名队伍数；成员口径=报名成员数（各队 member_count 求和）',
             'numerator': '至少 1 名成员签到的队伍 / attendance 表去重 member_ref 人数',
             'unit': 'team + member',
             'deduplication': '主键(event_id,team_id,member_ref)去重；同 request_key 重试安全重放，不重复计数'},
            {'stage': 'scored',
             'denominator': '到场队伍数（attended_teams）；未到场队伍以 not_scored_no_show 单列，不进分母',
             'numerator': '评分定案队伍（finalized）：裁判配置达标、评分卡齐全、无未决争议且评分会话已关闭；'
                          'completed_card_teams 单独统计仅卡片齐全但尚未定案的队伍',
             'unit': 'team'},
            {'stage': 'awarded', 'denominator': '完成评分队伍数（scored_teams）',
             'numerator': '组内 rank <= award_top_n 的队伍；同分并列共享名次',
             'unit': 'team'},
        ]

    @staticmethod
    def _validate_top_n(value: int) -> None:
        require(type(value) is int and 1 <= value <= 100, 'invalid_top_n',
                'award_top_n must be an integer from 1 to 100')

    # --------------------------------------------------------------------- csv
    def csv(self, report: dict) -> str:
        reason_cols = list(REASON_LEGEND.keys())
        header = ['event_id', 'basis', 'version', 'scope', 'district', 'group_code',
                  'registered_teams', 'attended_teams', 'completed_card_teams',
                  'scored_teams', 'awarded_teams',
                  'registered_members', 'arrived_members', 'member_no_shows',
                  'team_attendance_rate', 'member_attendance_rate',
                  'score_completion_rate', 'score_completion_rate_of_registered',
                  'award_conversion_rate'] + [f'reason_{code}' for code in reason_cols]
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(header)

        def write_row(scope_label: str, district: str, group_code: str, row: dict) -> None:
            writer.writerow([
                report['event_id'], report['basis'], report['version'], scope_label,
                district, group_code, row['registered_teams'], row['attended_teams'],
                row['completed_card_teams'], row['scored_teams'], row['awarded_teams'],
                row['registered_members'], row['arrived_members'], row['member_no_shows'],
                row['team_attendance_rate'], row['member_attendance_rate'],
                row['score_completion_rate'], row['score_completion_rate_of_registered'],
                row['award_conversion_rate'],
                *[row['reasons'].get(code, 0) for code in reason_cols]])

        write_row('TOTAL', 'ALL', 'ALL', report['totals'])
        for district in report['districts']:
            write_row('DISTRICT', district['district'], 'ALL', district)
        for group in report['groups']:
            group_districts = {team['district'] for team in group['teams_detail']}
            label = 'MIXED' if len(group_districts) > 1 else next(iter(group_districts), '')
            write_row('GROUP', label, group['group_code'], group)
        return stream.getvalue()
