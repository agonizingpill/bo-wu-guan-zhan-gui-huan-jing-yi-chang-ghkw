"""湿度短时越限累计与馆长复核。

规则口径：
- 同一展柜相邻两条有效采样间隔不超过 30 分钟；
- 前一条采样被算法判为离群，且相对湿度超出该展柜目标 RH 窗口，
  则两条采样之间的时长计为一条湿度越限时段；
- 未送审时段累计满 1 小时自动生成馆长复核送审单，重跑分析按样本对幂等，不重复计时；
- 主管只能整单结清送审时列明的时段；送审单创建后新入账的时段留待下次送审。
"""

import json
from threading import Lock

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .middlewares.auth import current_user, require_roles
from .utils.db import clean, fetch_all, fetch_one, get_conn

router = APIRouter(prefix='/humidity-reviews', tags=['湿度复核'])

SCHEMA_LOCK = Lock()
SCHEMA_READY = False

MAX_SAMPLE_GAP_SECONDS = 30 * 60
SUBMISSION_THRESHOLD_SECONDS = 60 * 60


def ensure_schema():
    global SCHEMA_READY
    with SCHEMA_LOCK:
        if SCHEMA_READY:
            return
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS humidity_breach_segments (
                        id SERIAL PRIMARY KEY,
                        case_id INTEGER NOT NULL REFERENCES display_cases(id) ON DELETE CASCADE,
                        start_sample_id INTEGER NOT NULL REFERENCES environment_samples(id) ON DELETE CASCADE,
                        end_sample_id INTEGER NOT NULL REFERENCES environment_samples(id) ON DELETE CASCADE,
                        started_at TIMESTAMPTZ NOT NULL,
                        ended_at TIMESTAMPTZ NOT NULL,
                        duration_seconds INTEGER NOT NULL,
                        status VARCHAR(20) NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending','submitted','settled')),
                        submission_id INTEGER,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        settled_at TIMESTAMPTZ,
                        UNIQUE(case_id, start_sample_id, end_sample_id),
                        CHECK (ended_at > started_at),
                        CHECK (duration_seconds > 0)
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS humidity_review_submissions (
                        id SERIAL PRIMARY KEY,
                        case_id INTEGER NOT NULL REFERENCES display_cases(id) ON DELETE CASCADE,
                        status VARCHAR(20) NOT NULL DEFAULT 'submitted'
                            CHECK (status IN ('submitted','settled')),
                        total_duration_seconds INTEGER NOT NULL CHECK (total_duration_seconds >= 3600),
                        segment_count INTEGER NOT NULL CHECK (segment_count > 0),
                        review_note TEXT,
                        submitted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        settled_by INTEGER REFERENCES users(id),
                        settled_at TIMESTAMPTZ
                    )
                """)
                cur.execute(
                    """
                    SELECT 1 FROM pg_constraint WHERE conname='humidity_segments_submission_fk'
                    """
                )
                if cur.fetchone() is None:
                    cur.execute(
                        'ALTER TABLE humidity_breach_segments ADD CONSTRAINT humidity_segments_submission_fk '
                        'FOREIGN KEY (submission_id) REFERENCES humidity_review_submissions(id) ON DELETE SET NULL'
                    )
                conn.commit()
        SCHEMA_READY = True


def sync_breach_segments(case_id: int, outlier_sample_ids: set[int]) -> int:
    """按当前一次算法分析的离群结果幂等入账湿度越限时段，返回新入账条数。"""
    ensure_schema()
    rows = fetch_all(
        '''
        WITH ordered AS (
            SELECT s.id, s.sampled_at, s.humidity,
                   LAG(s.id) OVER w AS prev_id,
                   LAG(s.sampled_at) OVER w AS prev_at,
                   LAG(s.humidity) OVER w AS prev_humidity,
                   c.target_rh_min, c.target_rh_max
            FROM environment_samples s
            JOIN display_cases c ON c.id = s.case_id
            WHERE s.case_id = %s AND s.state = 'accepted'
            WINDOW w AS (ORDER BY s.sampled_at)
        )
        SELECT * FROM ordered WHERE prev_id IS NOT NULL ORDER BY sampled_at
        ''',
        (case_id,),
    )
    candidates = []
    for row in rows:
        gap = int((row['sampled_at'] - row['prev_at']).total_seconds())
        # 同柜相邻采样间隔超过 30 分钟的缺测段不计入。
        if gap > MAX_SAMPLE_GAP_SECONDS:
            continue
        # 前一条采样必须被判离群且湿度越限，即把两条采样之间的时长计入。
        if row['prev_id'] not in outlier_sample_ids:
            continue
        prev_breach = not (float(row['target_rh_min']) <= float(row['prev_humidity']) <= float(row['target_rh_max']))
        if not prev_breach:
            continue
        candidates.append((case_id, row['prev_id'], row['id'], row['prev_at'], row['sampled_at'], gap))

    inserted = 0
    if candidates:
        # 按展柜串行化「时段入账 + 送审判断」，避免评分与送审/签认并发时新时段被卷入旧单。
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT pg_advisory_xact_lock(%s)', (case_id,))
                for candidate in candidates:
                    cur.execute(
                        '''
                        INSERT INTO humidity_breach_segments
                            (case_id, start_sample_id, end_sample_id, started_at, ended_at,
                             duration_seconds, status)
                        VALUES (%s, %s, %s, %s, %s, %s, 'pending')
                        ON CONFLICT (case_id, start_sample_id, end_sample_id) DO NOTHING
                        ''',
                        candidate,
                    )
                    inserted += cur.rowcount
                if inserted:
                    cur.execute(
                        """
                        SELECT COALESCE(SUM(duration_seconds), 0) AS total, COUNT(*) AS segment_count
                        FROM humidity_breach_segments
                        WHERE case_id=%s AND status='pending'
                        """,
                        (case_id,),
                    )
                    total, segment_count = cur.fetchone()
                    if segment_count and int(total) >= SUBMISSION_THRESHOLD_SECONDS:
                        cur.execute(
                            '''
                            INSERT INTO humidity_review_submissions
                                (case_id, status, total_duration_seconds, segment_count)
                            VALUES (%s, 'submitted', %s, %s) RETURNING id
                            ''',
                            (case_id, int(total), int(segment_count)),
                        )
                        submission_id = cur.fetchone()[0]
                        cur.execute(
                            """
                            UPDATE humidity_breach_segments SET status='submitted', submission_id=%s
                            WHERE case_id=%s AND status='pending'
                            """,
                            (submission_id, case_id),
                        )
            conn.commit()
    return inserted


def create_due_submissions(case_id: int) -> int:
    """未送审时段累计满 1 小时即生成馆长复核送审单，返回本次新建送审单数量。"""
    ensure_schema()
    with get_conn() as conn:
        with conn.cursor() as cur:
            # 与 sync_breach_segments 使用同一把展柜级咨询锁，串行化送审入账。
            cur.execute('SELECT pg_advisory_xact_lock(%s)', (case_id,))
            cur.execute('SELECT id FROM display_cases WHERE id=%s', (case_id,))
            if cur.fetchone() is None:
                conn.rollback()
                return 0
            cur.execute(
                """
                SELECT COALESCE(SUM(duration_seconds), 0) AS total, COUNT(*) AS segment_count
                FROM humidity_breach_segments
                WHERE case_id=%s AND status='pending'
                """,
                (case_id,),
            )
            total, segment_count = cur.fetchone()
            if segment_count == 0 or int(total) < SUBMISSION_THRESHOLD_SECONDS:
                conn.rollback()
                return 0
            cur.execute(
                '''
                INSERT INTO humidity_review_submissions
                    (case_id, status, total_duration_seconds, segment_count)
                VALUES (%s, 'submitted', %s, %s) RETURNING id
                ''',
                (case_id, int(total), int(segment_count)),
            )
            submission_id = cur.fetchone()[0]
            cur.execute(
                """
                UPDATE humidity_breach_segments SET status='submitted', submission_id=%s
                WHERE case_id=%s AND status='pending'
                """,
                (submission_id, case_id),
            )
            conn.commit()
    return 1


def settle_submission(submission_id: int, note: str, user: dict) -> dict:
    """主管整单结清：送审时列明的时段全部结清，不允许挑拣。"""
    ensure_schema()
    submission = fetch_one('SELECT * FROM humidity_review_submissions WHERE id=%s', (submission_id,))
    if not submission:
        raise HTTPException(404, '馆长复核送审单不存在')
    if submission['status'] == 'settled':
        raise HTTPException(409, '该送审单已结清，不能重复操作')
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT id FROM humidity_review_submissions WHERE id=%s FOR UPDATE', (submission_id,))
            cur.execute(
                '''
                UPDATE humidity_review_submissions
                SET status='settled', review_note=%s, settled_by=%s, settled_at=NOW()
                WHERE id=%s AND status='submitted'
                RETURNING id
                ''',
                (note, user['id'], submission_id),
            )
            if cur.fetchone() is None:
                conn.rollback()
                raise HTTPException(409, '该送审单已结清，不能重复操作')
            cur.execute(
                """
                UPDATE humidity_breach_segments SET status='settled', settled_at=NOW()
                WHERE submission_id=%s
                """,
                (submission_id,),
            )
            cur.execute(
                'INSERT INTO audit_logs (actor_id, action, entity_type, entity_id, detail) '
                "VALUES (%s,'settle_humidity_review','humidity_review_submission',%s,%s::jsonb)",
                (user['id'], submission_id, json.dumps(
                    {'case_id': submission['case_id'],
                     'segment_count': submission['segment_count'],
                     'note': note}, ensure_ascii=False)),
            )
            conn.commit()
    return fetch_one('SELECT * FROM humidity_review_submissions WHERE id=%s', (submission_id,))


def overview(case_id: int | None = None) -> list[dict]:
    ensure_schema()
    where = 'WHERE c.id = %s' if case_id else ''
    params = (case_id,) if case_id else ()
    return fetch_all(
        f'''
        SELECT c.id AS case_id, c.code AS case_code, c.artifact_title,
               c.target_rh_min, c.target_rh_max,
               COALESCE(summary.pending_seconds, 0) AS pending_seconds,
               COALESCE(summary.submitted_seconds, 0) AS submitted_seconds,
               COALESCE(summary.settled_seconds, 0) AS settled_seconds,
               COALESCE(summary.pending_count, 0) AS pending_count,
               COALESCE(summary.submitted_count, 0) AS submitted_count,
               COALESCE(summary.settled_count, 0) AS settled_count,
               CASE WHEN COALESCE(summary.pending_seconds, 0) >= %s THEN true ELSE false END AS due
        FROM display_cases c
        LEFT JOIN (
            SELECT case_id,
                   SUM(CASE WHEN status='pending' THEN duration_seconds ELSE 0 END) AS pending_seconds,
                   SUM(CASE WHEN status='submitted' THEN duration_seconds ELSE 0 END) AS submitted_seconds,
                   SUM(CASE WHEN status='settled' THEN duration_seconds ELSE 0 END) AS settled_seconds,
                   SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending_count,
                   SUM(CASE WHEN status='submitted' THEN 1 ELSE 0 END) AS submitted_count,
                   SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) AS settled_count
            FROM humidity_breach_segments GROUP BY case_id
        ) summary ON summary.case_id = c.id
        {where}
        ORDER BY c.code
        ''',
        (SUBMISSION_THRESHOLD_SECONDS, *(params if case_id else ())),
    )


def list_submissions(case_id: int | None = None) -> list[dict]:
    ensure_schema()
    sql = '''
        SELECT r.*, c.code AS case_code, c.artifact_title,
               u.display_name AS settled_by_name
        FROM humidity_review_submissions r
        JOIN display_cases c ON c.id = r.case_id
        LEFT JOIN users u ON u.id = r.settled_by
    '''
    params: tuple = ()
    if case_id:
        sql += ' WHERE r.case_id=%s'
        params = (case_id,)
    sql += ' ORDER BY r.submitted_at DESC, r.id DESC'
    return fetch_all(sql, params)


def list_segments(case_id: int) -> list[dict]:
    ensure_schema()
    return fetch_all(
        '''
        SELECT g.*, r.status AS submission_status, r.id AS review_submission_id,
               ps.sampled_at AS start_sample_at, pe.sampled_at AS end_sample_at,
               ps.humidity AS start_humidity, pe.humidity AS end_humidity
        FROM humidity_breach_segments g
        JOIN environment_samples ps ON ps.id = g.start_sample_id
        JOIN environment_samples pe ON pe.id = g.end_sample_id
        LEFT JOIN humidity_review_submissions r ON r.id = g.submission_id
        WHERE g.case_id=%s
        ORDER BY g.started_at DESC, g.id DESC
        ''',
        (case_id,),
    )


class SettlePayload(BaseModel):
    note: str = Field(min_length=3, max_length=500)


@router.get('/overview')
def get_overview(case_id: int | None = None, user=Depends(current_user)):
    return clean(overview(case_id))


@router.get('/submissions')
def get_submissions(case_id: int | None = None, user=Depends(current_user)):
    return clean(list_submissions(case_id))


@router.get('/cases/{case_id}/segments')
def get_segments(case_id: int, user=Depends(current_user)):
    return clean(list_segments(case_id))


@router.post('/submissions/{submission_id}/settle')
def settle(submission_id: int, payload: SettlePayload,
           user=Depends(require_roles('supervisor'))):
    row = settle_submission(submission_id, payload.note.strip(), user)
    return clean(row)
