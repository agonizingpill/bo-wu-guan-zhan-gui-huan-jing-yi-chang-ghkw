import hashlib
import os
from datetime import datetime, timedelta, timezone

from src.utils.db import execute, execute_returning, fetch_all, fetch_one, wait_for_db


def password(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def get_user(username, display_name, role):
    row = execute_returning(
        '''INSERT INTO users (username, display_name, role, password_hash)
           VALUES (%s,%s,%s,%s)
           ON CONFLICT (username) DO UPDATE SET display_name=EXCLUDED.display_name, role=EXCLUDED.role
           RETURNING id, username, display_name, role''',
        (username, display_name, role, password('museum123')),
    )
    return row or fetch_one('SELECT id, username, display_name, role FROM users WHERE username=%s', (username,))


def get_case(data):
    row = execute_returning(
        '''INSERT INTO display_cases
           (code, gallery, zone, artifact_code, artifact_title, target_temp_min, target_temp_max,
            target_rh_min, target_rh_max, status, door_sensor_id)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (code) DO UPDATE SET gallery=EXCLUDED.gallery, zone=EXCLUDED.zone,
             artifact_code=EXCLUDED.artifact_code, artifact_title=EXCLUDED.artifact_title
           RETURNING id''',
        data,
    )
    return row['id'] if row else fetch_one('SELECT id FROM display_cases WHERE code=%s', (data[0],))['id']


def sample(case_id, when, temp, rh, co2, door, note):
    row = execute_returning(
        '''INSERT INTO environment_samples
           (case_id, sampled_at, temperature, humidity, co2_ppm, door_open, source, state, note, created_by)
           VALUES (%s,%s,%s,%s,%s,%s,'seed','accepted',%s,NULL)
           ON CONFLICT (case_id, sampled_at) DO NOTHING RETURNING id''',
        (case_id, when, temp, rh, co2, door, note),
    )
    if row:
        return row['id']
    return fetch_one('SELECT id FROM environment_samples WHERE case_id=%s AND sampled_at=%s', (case_id, when))['id']


def seed_event(case_id, sample_id, score, threshold, anomaly_type, severity, status, door_context, consecutive, escalated, reason):
    existing = fetch_one('SELECT id FROM anomaly_events WHERE sample_id=%s', (sample_id,))
    if existing:
        return existing['id']
    row = execute_returning(
        '''INSERT INTO anomaly_events
           (case_id, sample_id, score, threshold, anomaly_type, severity, status, door_context,
            consecutive_count, escalated_to_curator, reason)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
        (case_id, sample_id, score, threshold, anomaly_type, severity, status, door_context, consecutive, escalated, reason),
    )
    return row['id']


def seed_humidity_breach_demo(cases, base):
    """湿度短时越限累计演示：7 段越限（每段 10 分钟），前 6 段已送审并结清，第 7 段留待下次送审。"""
    from src.humidity_review import ensure_schema

    case_id = get_case(('MC-05', '书画展厅', '二层 02 号展柜', 'PM-102', '明代绢本设色山水轴',
                        19.0, 23.0, 45.0, 55.0, 'active', 'door-mc05'))
    cases['MC-05'] = case_id
    for index, values in enumerate([(21.4, 58.2, 640), (21.5, 58.9, 655), (21.6, 59.4, 660),
                                    (21.7, 60.1, 672), (21.8, 60.6, 680), (21.9, 61.0, 690),
                                    (22.0, 61.3, 695), (22.0, 61.1, 690)]):
        sample(case_id, base + timedelta(minutes=index * 10), *values, False, '湿度短时反复越限观察样本')
    ensure_schema()
    sample_ids = [row['id'] for row in fetch_all(
        'SELECT id FROM environment_samples WHERE case_id=%s ORDER BY sampled_at', (case_id,))]
    pair_rows = fetch_all(
        'SELECT sampled_at, LEAD(sampled_at) OVER (ORDER BY sampled_at) AS next_at FROM environment_samples '
        'WHERE case_id=%s ORDER BY sampled_at', (case_id,))
    for index in range(len(sample_ids) - 1):
        execute(
            '''INSERT INTO humidity_breach_segments
               (case_id, start_sample_id, end_sample_id, started_at, ended_at, duration_seconds, status)
               VALUES (%s,%s,%s,%s,%s,%s,'pending')
               ON CONFLICT (case_id, start_sample_id, end_sample_id) DO NOTHING''',
            (case_id, sample_ids[index], sample_ids[index + 1],
             pair_rows[index]['sampled_at'], pair_rows[index]['next_at'], 600),
        )
    existing = fetch_one('SELECT id FROM humidity_review_submissions WHERE case_id=%s ORDER BY id LIMIT 1', (case_id,))
    if existing:
        return
    supervisor = fetch_one("SELECT id FROM users WHERE username='guo-supervisor'")
    first_six = sample_ids[:6]
    submission = execute_returning(
        '''INSERT INTO humidity_review_submissions
           (case_id, status, total_duration_seconds, segment_count, review_note,
            submitted_at, settled_by, settled_at)
           VALUES (%s,'settled',3600,6,'夜班复核：湿度短时反复越限一小时，文物外观无异常，已调整恒湿机设定',
                   %s, %s, %s) RETURNING id''',
        (case_id, base + timedelta(minutes=65), supervisor['id'], base + timedelta(minutes=80)),
    )
    execute(
        '''UPDATE humidity_breach_segments SET status='settled', submission_id=%s, settled_at=%s
           WHERE case_id=%s AND start_sample_id = ANY(%s)''',
        (submission['id'], base + timedelta(minutes=80), case_id, first_six),
    )


def main():
    wait_for_db()
    operator = get_user('lin-operator', '林芮 · 夜班值守', 'operator')
    supervisor = get_user('guo-supervisor', '郭岚 · 藏品主管', 'supervisor')
    get_user('viewer', '顾言 · 研究阅览', 'viewer')

    cases = {}
    cases['MC-01'] = get_case(('MC-01', '青铜器展厅', '东侧 02 号展柜', 'BR-018', '错金银云纹铜犀尊', 19.0, 23.0, 45.0, 55.0, 'active', 'door-mc01'))
    cases['MC-02'] = get_case(('MC-02', '丝路文物展厅', '西侧 07 号展柜', 'SR-044', '唐三彩骆驼俑', 18.0, 22.0, 42.0, 52.0, 'active', 'door-mc02'))
    cases['MC-03'] = get_case(('MC-03', '玉器展厅', '北侧 03 号展柜', 'JA-006', '汉代龙纹玉璧', 20.0, 24.0, 40.0, 50.0, 'active', 'door-mc03'))
    cases['MC-04'] = get_case(('MC-04', '瓷器展厅', '南侧 05 号展柜', 'PO-071', '北宋汝窑天青釉盘', 18.5, 22.5, 44.0, 54.0, 'active', 'door-mc04'))

    base = datetime(2026, 11, 21, 18, 0, tzinfo=timezone.utc)
    for index, (temp, rh, co2) in enumerate([(21.0, 49.0, 620), (21.2, 49.5, 640), (20.9, 48.8, 610), (21.1, 49.2, 630), (21.0, 49.1, 625), (21.2, 49.3, 635), (21.1, 49.0, 615), (21.0, 49.2, 628)]):
        sample(cases['MC-01'], base + timedelta(minutes=index * 10), temp, rh, co2, False, '夜班稳态基线')

    door_ids = []
    for index, values in enumerate([(20.4, 47.2, 720, False), (20.3, 47.0, 715, True), (22.8, 60.0, 1200, True), (22.5, 59.0, 1150, True), (21.0, 50.0, 700, False), (21.1, 49.6, 680, False), (21.0, 49.5, 675, False)]):
        door_ids.append(sample(cases['MC-02'], base + timedelta(minutes=index * 10), *values, '门磁开启期间的光照调整'))
    seed_event(cases['MC-02'], door_ids[2], -0.14, -0.10, 'door_interference', 'watch', 'open', True, 1, False, '门磁开启段内温湿度突变，等待关门后复采')

    drift_ids = []
    for index, values in enumerate([(21.2, 45.0, 650), (21.4, 45.2, 665), (23.8, 57.0, 1100), (24.4, 59.0, 1240), (24.8, 61.0, 1370), (21.8, 47.0, 700), (21.4, 46.0, 680)]):
        drift_ids.append(sample(cases['MC-03'], base + timedelta(minutes=index * 10), *values, False, '连续偏移观察样本'))
    seed_event(cases['MC-03'], drift_ids[4], -0.31, -0.12, 'continuous_drift', 'critical', 'escalated', False, 3, True, '连续三个采样点超出稳态基线，已升级馆长查看')

    for index, values in enumerate([(20.1, 50.0, 590), (20.0, 50.2, 605), (20.2, 49.8, 600), (20.1, 50.1, 610), (20.0, 49.9, 600), (20.2, 50.0, 615)]):
        sample(cases['MC-04'], base + timedelta(minutes=index * 10), *values, False, '日常巡检基线')

    seed_humidity_breach_demo(cases, base)

    execute("UPDATE display_cases SET last_sample_at=(SELECT MAX(sampled_at) FROM environment_samples WHERE case_id=display_cases.id)")
    execute(
        '''INSERT INTO audit_logs (actor_id, action, entity_type, entity_id, detail)
           SELECT %s, 'seed_demo', 'batch', NULL, '{"source":"seed_demo","batch":"night-shift-2026-11-21"}'::jsonb
           WHERE NOT EXISTS (SELECT 1 FROM audit_logs WHERE action='seed_demo' AND detail->>'batch'='night-shift-2026-11-21')''',
        (operator['id'],),
    )
    print('seed_demo complete: 5 display cases, 36 samples, 2 seeded anomaly events, 7 humidity breach segments, 3 users')


if __name__ == '__main__':
    main()
