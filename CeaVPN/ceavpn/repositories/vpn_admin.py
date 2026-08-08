from __future__ import annotations

import sqlite3
from typing import Any, Dict, List

from ceavpn.repositories.base import row_to_dict, rows_to_dicts


class VpnAdminRepository:
    def dashboard_stats(
        self,
        conn: sqlite3.Connection,
        *,
        now: str,
        period_started_at: str,
        healthy_after: str,
    ) -> Dict[str, Any]:
        return (
            row_to_dict(
                conn.execute(
                    """
                    SELECT
                        (
                            SELECT COUNT(DISTINCT u.id)
                            FROM users u
                            WHERE EXISTS (
                                SELECT 1 FROM vpn_subscriptions s
                                WHERE s.user_id = u.id
                            ) OR EXISTS (
                                SELECT 1 FROM vpn_payments pay
                                WHERE pay.user_id = u.id
                            ) OR EXISTS (
                                SELECT 1 FROM vpn_trial_claims claim
                                WHERE claim.user_id = u.id
                            )
                        ) AS users_total,
                        (
                            SELECT COUNT(DISTINCT s.user_id)
                            FROM vpn_subscriptions s
                            WHERE s.status = 'active' AND s.ends_at > ?
                        ) AS active_users,
                        (
                            SELECT COUNT(DISTINCT s.user_id)
                            FROM vpn_subscriptions s
                            WHERE s.status = 'active'
                              AND s.ends_at > ?
                              AND s.billing_kind = 'paid'
                        ) AS active_paid_users,
                        (
                            SELECT COUNT(DISTINCT s.user_id)
                            FROM vpn_subscriptions s
                            WHERE s.status = 'active'
                              AND s.ends_at > ?
                              AND s.billing_kind = 'trial'
                        ) AS active_trial_users,
                        (
                            SELECT COUNT(*) FROM vpn_trial_claims
                        ) AS trial_users,
                        (
                            SELECT COUNT(DISTINCT pay.user_id)
                            FROM vpn_payments pay
                            WHERE pay.status IN ('paid', 'completed', 'confirmed')
                              AND pay.provider <> 'admin_demo'
                        ) AS paid_users,
                        (
                            SELECT COUNT(*) FROM vpn_payments pay
                            WHERE pay.status IN ('paid', 'completed', 'confirmed')
                              AND pay.provider <> 'admin_demo'
                        ) AS paid_payments,
                        (
                            SELECT COALESCE(SUM(pay.amount_rub), 0)
                            FROM vpn_payments pay
                            WHERE pay.status IN ('paid', 'completed', 'confirmed')
                              AND pay.provider <> 'admin_demo'
                        ) AS revenue_rub,
                        (
                            SELECT COALESCE(SUM(pay.amount_rub), 0)
                            FROM vpn_payments pay
                            WHERE pay.status IN ('paid', 'completed', 'confirmed')
                              AND pay.provider <> 'admin_demo'
                              AND REPLACE(COALESCE(pay.paid_at, pay.updated_at, pay.created_at), ' ', 'T') >= ?
                        ) AS revenue_period_rub,
                        (
                            SELECT COUNT(*) FROM vpn_subscriptions s
                            WHERE s.status = 'expired'
                               OR (s.status = 'active' AND s.ends_at <= ?)
                        ) AS expired_subscriptions,
                        (
                            SELECT COUNT(*) FROM vpn_subscriptions s
                            WHERE s.status = 'provisioning'
                        ) AS provisioning_subscriptions,
                        (
                            SELECT COUNT(*) FROM vpn_subscriptions s
                            WHERE s.status = 'error'
                        ) AS error_subscriptions,
                        (
                            SELECT COUNT(*) FROM vpn_provisioning_jobs job
                            WHERE job.status = 'failed'
                        ) AS failed_jobs,
                        (
                            SELECT COUNT(*) FROM vpn_servers srv
                            WHERE srv.is_active = TRUE
                        ) AS servers_total,
                        (
                            SELECT COUNT(*) FROM vpn_servers srv
                            WHERE srv.is_active = TRUE
                              AND srv.last_health_at IS NOT NULL
                              AND REPLACE(srv.last_health_at, ' ', 'T') >= ?
                        ) AS servers_healthy,
                        (
                            SELECT COUNT(*) FROM vpn_user_bans ban
                            WHERE ban.is_active = TRUE
                        ) AS blocked_users
                    """,
                    (
                        now,
                        now,
                        now,
                        period_started_at,
                        now,
                        healthy_after,
                    ),
                ).fetchone()
            )
            or {}
        )

    def list_servers(
        self,
        conn: sqlite3.Connection,
        *,
        now: str,
        healthy_after: str,
    ) -> List[Dict[str, Any]]:
        return rows_to_dicts(
            conn.execute(
                """
                SELECT
                    srv.*,
                    CASE
                        WHEN srv.is_active = TRUE
                         AND srv.last_health_at IS NOT NULL
                         AND REPLACE(srv.last_health_at, ' ', 'T') >= ?
                        THEN TRUE ELSE FALSE
                    END AS is_healthy,
                    (
                        SELECT COUNT(*) FROM vpn_subscriptions s
                        WHERE s.server_id = srv.id
                          AND s.status = 'active'
                          AND s.ends_at > ?
                    ) AS active_subscriptions,
                    (
                        SELECT COUNT(*) FROM vpn_provisioning_jobs job
                        WHERE job.server_id = srv.id
                          AND job.status IN ('pending', 'running')
                    ) AS queued_jobs,
                    (
                        SELECT COUNT(*) FROM vpn_provisioning_jobs job
                        WHERE job.server_id = srv.id
                          AND job.status = 'failed'
                    ) AS failed_jobs
                FROM vpn_servers srv
                ORDER BY srv.is_active DESC, srv.code ASC
                """,
                (healthy_after, now),
            ).fetchall()
        )

    @staticmethod
    def _segment_filter(segment: str) -> tuple[str, list[Any]]:
        filters: dict[str, tuple[str, list[Any]]] = {
            "all": ("", []),
            "trial": (
                """
                AND EXISTS (
                    SELECT 1 FROM vpn_trial_claims claim_filter
                    WHERE claim_filter.user_id = u.id
                )
                """,
                [],
            ),
            "paid": (
                """
                AND EXISTS (
                    SELECT 1 FROM vpn_payments pay_filter
                    WHERE pay_filter.user_id = u.id
                      AND pay_filter.status = 'paid'
                      AND pay_filter.provider <> 'admin_demo'
                )
                """,
                [],
            ),
            "active": (
                """
                AND EXISTS (
                    SELECT 1 FROM vpn_subscriptions active_filter
                    WHERE active_filter.user_id = u.id
                      AND active_filter.status = 'active'
                      AND active_filter.ends_at > ?
                )
                """,
                ["now"],
            ),
            "expired": (
                """
                AND EXISTS (
                    SELECT 1 FROM vpn_subscriptions expired_filter
                    WHERE expired_filter.user_id = u.id
                )
                AND NOT EXISTS (
                    SELECT 1 FROM vpn_subscriptions active_filter
                    WHERE active_filter.user_id = u.id
                      AND active_filter.status = 'active'
                      AND active_filter.ends_at > ?
                )
                """,
                ["now"],
            ),
            "issues": (
                """
                AND (
                    EXISTS (
                        SELECT 1 FROM vpn_subscriptions error_filter
                        WHERE error_filter.user_id = u.id
                          AND error_filter.status = 'error'
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM vpn_provisioning_jobs job_filter
                        JOIN vpn_subscriptions job_subscription
                          ON job_subscription.id = job_filter.subscription_id
                        WHERE job_subscription.user_id = u.id
                          AND job_filter.status = 'failed'
                    )
                )
                """,
                [],
            ),
            "blocked": (
                """
                AND EXISTS (
                    SELECT 1 FROM vpn_user_bans ban_filter
                    WHERE ban_filter.user_id = u.id
                      AND ban_filter.is_active = TRUE
                )
                """,
                [],
            ),
        }
        return filters.get(segment, filters["all"])

    @staticmethod
    def _search_filter(query: str) -> tuple[str, list[Any]]:
        normalized = query.strip().lstrip("@").lower()
        if not normalized:
            return "", []
        pattern = f"%{normalized}%"
        return (
            """
            AND (
                LOWER(COALESCE(u.username, '')) LIKE ?
                OR LOWER(COALESCE(u.first_name, '')) LIKE ?
                OR LOWER(COALESCE(u.last_name, '')) LIKE ?
                OR CAST(u.telegram_id AS TEXT) LIKE ?
                OR CAST(u.id AS TEXT) LIKE ?
            )
            """,
            [pattern, pattern, pattern, pattern, pattern],
        )

    def count_users(
        self,
        conn: sqlite3.Connection,
        *,
        query: str,
        segment: str,
        now: str,
    ) -> int:
        search_sql, search_params = self._search_filter(query)
        segment_sql, segment_params = self._segment_filter(segment)
        params = [
            *(now if value == "now" else value for value in segment_params),
            *search_params,
        ]
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM users u
            WHERE (
                EXISTS (
                    SELECT 1 FROM vpn_subscriptions s_scope
                    WHERE s_scope.user_id = u.id
                )
                OR EXISTS (
                    SELECT 1 FROM vpn_payments pay_scope
                    WHERE pay_scope.user_id = u.id
                )
                OR EXISTS (
                    SELECT 1 FROM vpn_trial_claims claim_scope
                    WHERE claim_scope.user_id = u.id
                )
            )
            {segment_sql}
            {search_sql}
            """,
            tuple(params),
        ).fetchone()
        return int(row["count"] if row else 0)

    def list_users(
        self,
        conn: sqlite3.Connection,
        *,
        page: int,
        page_size: int,
        query: str,
        segment: str,
        now: str,
    ) -> List[Dict[str, Any]]:
        search_sql, search_params = self._search_filter(query)
        segment_sql, segment_params = self._segment_filter(segment)
        params = [
            now,
            now,
            *(now if value == "now" else value for value in segment_params),
            *search_params,
            page_size,
            max(page - 1, 0) * page_size,
        ]
        return rows_to_dicts(
            conn.execute(
                f"""
                SELECT
                    u.*,
                    s.id AS vpn_subscription_id,
                    s.status AS vpn_status,
                    s.billing_kind AS vpn_billing_kind,
                    s.starts_at AS vpn_starts_at,
                    s.ends_at AS vpn_ends_at,
                    s.last_error AS vpn_last_error,
                    ban.is_active AS vpn_is_blocked,
                    ban.reason AS vpn_block_reason,
                    ban.updated_at AS vpn_blocked_at,
                    p.name AS vpn_plan_name,
                    CASE
                        WHEN s.status = 'active' AND s.ends_at > ?
                        THEN TRUE ELSE FALSE
                    END AS vpn_is_active,
                    EXISTS (
                        SELECT 1 FROM vpn_trial_claims claim
                        WHERE claim.user_id = u.id
                    ) AS vpn_has_trial,
                    EXISTS (
                        SELECT 1 FROM vpn_payments paid
                        WHERE paid.user_id = u.id
                          AND paid.status = 'paid'
                          AND paid.provider <> 'admin_demo'
                    ) AS vpn_has_paid,
                    (
                        SELECT COUNT(*) FROM vpn_payments paid
                        WHERE paid.user_id = u.id
                          AND paid.status = 'paid'
                          AND paid.provider <> 'admin_demo'
                    ) AS vpn_paid_count,
                    (
                        SELECT COALESCE(SUM(paid.amount_rub), 0)
                        FROM vpn_payments paid
                        WHERE paid.user_id = u.id
                          AND paid.status = 'paid'
                          AND paid.provider <> 'admin_demo'
                    ) AS vpn_paid_amount_rub,
                    (
                        SELECT MAX(paid.paid_at)
                        FROM vpn_payments paid
                        WHERE paid.user_id = u.id
                          AND paid.status = 'paid'
                          AND paid.provider <> 'admin_demo'
                    ) AS vpn_last_paid_at
                FROM users u
                LEFT JOIN vpn_subscriptions s
                  ON s.id = (
                    SELECT latest.id
                    FROM vpn_subscriptions latest
                    WHERE latest.user_id = u.id
                    ORDER BY
                        CASE
                            WHEN latest.status = 'active'
                             AND latest.ends_at > ?
                            THEN 0
                            WHEN latest.status = 'provisioning' THEN 1
                            WHEN latest.status = 'error' THEN 2
                            ELSE 3
                        END,
                        latest.created_at DESC
                    LIMIT 1
                  )
                LEFT JOIN vpn_plans p ON p.id = s.plan_id
                LEFT JOIN vpn_user_bans ban
                  ON ban.user_id = u.id
                WHERE (
                    EXISTS (
                        SELECT 1 FROM vpn_subscriptions s_scope
                        WHERE s_scope.user_id = u.id
                    )
                    OR EXISTS (
                        SELECT 1 FROM vpn_payments pay_scope
                        WHERE pay_scope.user_id = u.id
                    )
                    OR EXISTS (
                        SELECT 1 FROM vpn_trial_claims claim_scope
                        WHERE claim_scope.user_id = u.id
                    )
                )
                {segment_sql}
                {search_sql}
                ORDER BY u.created_at DESC
                LIMIT ? OFFSET ?
                """,
                tuple(params),
            ).fetchall()
        )

    def user_card(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: int,
        now: str,
    ) -> Dict[str, Any] | None:
        user = row_to_dict(
            conn.execute(
                """
                SELECT * FROM users u
                WHERE u.id = ?
                  AND (
                    EXISTS (
                        SELECT 1 FROM vpn_subscriptions s
                        WHERE s.user_id = u.id
                    )
                    OR EXISTS (
                        SELECT 1 FROM vpn_payments pay
                        WHERE pay.user_id = u.id
                    )
                    OR EXISTS (
                        SELECT 1 FROM vpn_trial_claims claim
                        WHERE claim.user_id = u.id
                    )
                  )
                """,
                (user_id,),
            ).fetchone()
        )
        if user is None:
            return None

        subscription = row_to_dict(
            conn.execute(
                """
                SELECT
                    s.*,
                    p.name AS plan_name,
                    p.code AS plan_code,
                    p.max_devices,
                    srv.name AS server_name,
                    srv.region AS server_region,
                    ban.is_active AS is_blocked,
                    ban.reason AS block_reason,
                    ban.updated_at AS blocked_at
                FROM vpn_subscriptions s
                LEFT JOIN vpn_plans p ON p.id = s.plan_id
                JOIN vpn_servers srv ON srv.id = s.server_id
                LEFT JOIN vpn_user_bans ban ON ban.user_id = s.user_id
                WHERE s.user_id = ?
                ORDER BY
                    CASE
                        WHEN s.status = 'active' AND s.ends_at > ? THEN 0
                        WHEN s.status = 'provisioning' THEN 1
                        WHEN s.status = 'error' THEN 2
                        ELSE 3
                    END,
                    s.created_at DESC
                LIMIT 1
                """,
                (user_id, now),
            ).fetchone()
        )
        trial = row_to_dict(
            conn.execute(
                """
                SELECT * FROM vpn_trial_claims
                WHERE user_id = ?
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
        )
        payments = (
            row_to_dict(
                conn.execute(
                    """
                    SELECT
                        COUNT(*) AS paid_count,
                        COALESCE(SUM(amount_rub), 0) AS paid_amount_rub,
                        MAX(paid_at) AS last_paid_at
                    FROM vpn_payments
                    WHERE user_id = ?
                      AND status = 'paid'
                      AND provider <> 'admin_demo'
                    """,
                    (user_id,),
                ).fetchone()
            )
            or {}
        )
        recent_payments = rows_to_dicts(
            conn.execute(
                """
                SELECT pay.*, plan.name AS plan_name
                FROM vpn_payments pay
                JOIN vpn_plans plan ON plan.id = pay.vpn_plan_id
                WHERE pay.user_id = ?
                ORDER BY pay.created_at DESC
                LIMIT 10
                """,
                (user_id,),
            ).fetchall()
        )
        recent_jobs: List[Dict[str, Any]] = []
        if subscription is not None:
            recent_jobs = rows_to_dicts(
                conn.execute(
                    """
                    SELECT job.*, srv.name AS server_name
                    FROM vpn_provisioning_jobs job
                    LEFT JOIN vpn_servers srv ON srv.id = job.server_id
                    WHERE job.subscription_id = ?
                    ORDER BY job.created_at DESC
                    LIMIT 10
                    """,
                    (int(subscription["id"]),),
                ).fetchall()
            )
        user["subscription"] = subscription
        user["trial"] = trial
        user["vpn_ban"] = self._active_ban(conn, user_id)
        user["payments"] = payments
        user["recent_payments"] = recent_payments
        user["recent_jobs"] = recent_jobs
        return user

    def _active_ban(
        self,
        conn: sqlite3.Connection,
        user_id: int,
    ) -> Dict[str, Any] | None:
        return row_to_dict(
            conn.execute(
                """
                SELECT *
                FROM vpn_user_bans
                WHERE user_id = ?
                  AND is_active = TRUE
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
        )
