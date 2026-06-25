"""add WQ alpha ledger and failure memory

Revision ID: 012
Revises: 011
Create Date: 2026-05-15
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "012"
down_revision: Union[str, None] = "011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "wq_alpha_experiments",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), nullable=True),
        sa.Column("alpha_id", sa.String(50), nullable=True),
        sa.Column("expression", sa.Text(), nullable=False),
        sa.Column("expression_normalized", sa.Text(), nullable=False),
        sa.Column("expression_hash", sa.String(64), nullable=False),
        sa.Column("params_hash", sa.String(64), nullable=False),
        sa.Column("account", sa.String(50), server_default="primary", nullable=False),
        sa.Column("region", sa.String(10), server_default="USA", nullable=False),
        sa.Column("universe", sa.String(20), server_default="TOP3000", nullable=False),
        sa.Column("delay", sa.Integer(), server_default="1", nullable=False),
        sa.Column("decay", sa.Integer(), server_default="0", nullable=False),
        sa.Column("neutralization", sa.String(30), server_default="SUBINDUSTRY", nullable=False),
        sa.Column("truncation", sa.Float(), server_default="0.08", nullable=False),
        sa.Column("source_type", sa.String(50), nullable=True),
        sa.Column("source_family", sa.String(100), nullable=True),
        sa.Column("source_run_id", sa.String(200), nullable=True),
        sa.Column("source_file", sa.String(500), nullable=True),
        sa.Column("source_tag", sa.String(100), nullable=True),
        sa.Column("parent_experiment_id", UUID(as_uuid=True), nullable=True),
        sa.Column("candidate_meta", sa.JSON(), nullable=True),
        sa.Column("lifecycle_status", sa.String(40), server_default="candidate", nullable=False),
        sa.Column("submit_eligible", sa.Boolean(), nullable=True),
        sa.Column("non_correlation_pass", sa.Boolean(), nullable=True),
        sa.Column("api_check_status", sa.String(50), nullable=True),
        sa.Column("platform_status", sa.String(50), nullable=True),
        sa.Column("review_failure_kind", sa.String(50), nullable=True),
        sa.Column("sharpe", sa.Float(), nullable=True),
        sa.Column("fitness", sa.Float(), nullable=True),
        sa.Column("returns", sa.Float(), nullable=True),
        sa.Column("turnover", sa.Float(), nullable=True),
        sa.Column("drawdown", sa.Float(), nullable=True),
        sa.Column("margin", sa.Float(), nullable=True),
        sa.Column("long_count", sa.Integer(), nullable=True),
        sa.Column("short_count", sa.Integer(), nullable=True),
        sa.Column("grade", sa.String(30), nullable=True),
        sa.Column("self_correlation_result", sa.String(30), nullable=True),
        sa.Column("self_correlation_value", sa.Float(), nullable=True),
        sa.Column("self_correlation_limit", sa.Float(), nullable=True),
        sa.Column("prod_correlation_result", sa.String(30), nullable=True),
        sa.Column("prod_correlation_value", sa.Float(), nullable=True),
        sa.Column("prod_correlation_limit", sa.Float(), nullable=True),
        sa.Column("max_similarity_to_blocked", sa.Float(), nullable=True),
        sa.Column("max_similarity_to_hits", sa.Float(), nullable=True),
        sa.Column("nearest_blocked_alpha_id", sa.String(50), nullable=True),
        sa.Column("nearest_blocked_expression", sa.Text(), nullable=True),
        sa.Column("nearest_blocked_source", sa.String(100), nullable=True),
        sa.Column("similarity_details", sa.JSON(), nullable=True),
        sa.Column("failure_kind", sa.String(50), nullable=True),
        sa.Column("failure_reasons", sa.JSON(), nullable=True),
        sa.Column("raw_result", sa.JSON(), nullable=True),
        sa.Column("raw_api_check", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
    )
    op.create_index("ix_wq_alpha_experiments_user_id", "wq_alpha_experiments", ["user_id"])
    op.create_index("ix_wq_alpha_experiments_alpha_id", "wq_alpha_experiments", ["alpha_id"])
    op.create_index("ix_wq_alpha_experiments_expression_hash", "wq_alpha_experiments", ["expression_hash"])
    op.create_index("ix_wq_alpha_experiments_params_hash", "wq_alpha_experiments", ["params_hash"])
    op.create_index("ix_wq_alpha_experiments_source_run_id", "wq_alpha_experiments", ["source_run_id"])
    op.create_index("ix_wq_alpha_experiments_lifecycle_status", "wq_alpha_experiments", ["lifecycle_status"])
    op.create_index("ix_wq_alpha_experiments_api_check_status", "wq_alpha_experiments", ["api_check_status"])
    op.create_index("ix_wq_alpha_experiments_fitness", "wq_alpha_experiments", ["fitness"])
    op.create_index("ix_wq_alpha_experiments_failure_kind", "wq_alpha_experiments", ["failure_kind"])
    op.create_index(
        "ix_wq_alpha_experiments_expr_params_run",
        "wq_alpha_experiments",
        ["expression_hash", "params_hash", "source_run_id"],
    )
    op.create_index(
        "ix_wq_alpha_experiments_status_fitness",
        "wq_alpha_experiments",
        ["lifecycle_status", "fitness"],
    )
    op.create_index(
        "ix_wq_alpha_experiments_source_family",
        "wq_alpha_experiments",
        ["source_family", "created_at"],
    )

    op.create_table(
        "wq_failure_memory",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), nullable=True),
        sa.Column("experiment_id", UUID(as_uuid=True), nullable=True),
        sa.Column("memory_type", sa.String(40), nullable=False),
        sa.Column("scope", sa.String(100), server_default="global", nullable=False),
        sa.Column("expression", sa.Text(), nullable=True),
        sa.Column("expression_normalized", sa.Text(), nullable=True),
        sa.Column("expression_hash", sa.String(64), nullable=True),
        sa.Column("pattern_signature", sa.String(500), nullable=True),
        sa.Column("fields", sa.JSON(), nullable=True),
        sa.Column("operators", sa.JSON(), nullable=True),
        sa.Column("params", sa.JSON(), nullable=True),
        sa.Column("failure_kind", sa.String(50), nullable=False),
        sa.Column("severity", sa.String(20), server_default="note", nullable=False),
        sa.Column("confidence", sa.Float(), server_default="1.0", nullable=False),
        sa.Column("evidence_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.Column("source_experiment_ids", sa.JSON(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
    )
    op.create_index("ix_wq_failure_memory_user_id", "wq_failure_memory", ["user_id"])
    op.create_index("ix_wq_failure_memory_experiment_id", "wq_failure_memory", ["experiment_id"])
    op.create_index("ix_wq_failure_memory_memory_type", "wq_failure_memory", ["memory_type"])
    op.create_index("ix_wq_failure_memory_expression_hash", "wq_failure_memory", ["expression_hash"])
    op.create_index("ix_wq_failure_memory_pattern_signature", "wq_failure_memory", ["pattern_signature"])
    op.create_index("ix_wq_failure_memory_failure_kind", "wq_failure_memory", ["failure_kind"])
    op.create_index("ix_wq_failure_memory_severity", "wq_failure_memory", ["severity"])
    op.create_index("ix_wq_failure_memory_kind_severity", "wq_failure_memory", ["failure_kind", "severity"])
    op.create_index(
        "ix_wq_failure_memory_type_kind",
        "wq_failure_memory",
        ["memory_type", "failure_kind", "severity"],
    )


def downgrade() -> None:
    op.drop_table("wq_failure_memory")
    op.drop_table("wq_alpha_experiments")
