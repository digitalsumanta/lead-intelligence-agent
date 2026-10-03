"""buying_signal_detect

Revision ID: 0033_buying_signal_detect
Revises: 0032_digital_marketing_intel
Create Date: 2026-10-03 16:30:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "0033_buying_signal_detect"
down_revision = "0032_digital_marketing_intel"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Create the buying_signals table.

    Existing shared PostgreSQL enums:
    - confidencelevel
    - sourcetype

    are reused and must NOT be created again.

    signalstrength is introduced by this migration and is created
    if it does not already exist.
    """

    # ------------------------------------------------------------------
    # SignalStrength enum
    # ------------------------------------------------------------------
    signalstrength = postgresql.ENUM(
        "LOW",
        "MEDIUM",
        "HIGH",
        name="signalstrength",
    )

    signalstrength.create(
        op.get_bind(),
        checkfirst=True,
    )

    # ------------------------------------------------------------------
    # Buying signals table
    # ------------------------------------------------------------------
    op.create_table(
        "buying_signals",

        sa.Column(
            "id",
            sa.Uuid(),
            nullable=False,
        ),

        sa.Column(
            "company_id",
            sa.Uuid(),
            nullable=False,
        ),

        sa.Column(
            "signal_type",
            sa.String(length=100),
            nullable=False,
        ),

        sa.Column(
            "signal_category",
            sa.String(length=100),
            nullable=False,
        ),

        # Existing shared enum.
        # DO NOT attempt to create confidencelevel again.
        sa.Column(
            "confidence",
            postgresql.ENUM(
                "UNKNOWN",
                "LOW",
                "MEDIUM",
                "HIGH",
                name="confidencelevel",
                create_type=False,
            ),
            nullable=False,
        ),

        # Enum introduced by this migration.
        # It was created above, so do not create it again here.
        sa.Column(
            "strength",
            postgresql.ENUM(
                "LOW",
                "MEDIUM",
                "HIGH",
                name="signalstrength",
                create_type=False,
            ),
            nullable=True,
        ),

        # Existing shared enum.
        # DO NOT attempt to create sourcetype again.
        sa.Column(
            "source_type",
            postgresql.ENUM(
                "LEAD_RECORD",
                "COMPANY_RECORD",
                "CONTACT_RECORD",
                "USER_PROVIDED",
                "SYSTEM_DERIVED",
                name="sourcetype",
                create_type=False,
            ),
            nullable=False,
        ),

        sa.Column(
            "detected_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),

        sa.Column(
            "evidence",
            sa.JSON(),
            nullable=False,
        ),

        sa.Column(
            "status",
            sa.String(length=50),
            nullable=False,
        ),

        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),

        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),

        # Company identity is represented by the existing leads table.
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["leads.id"],
            ondelete="CASCADE",
        ),

        sa.PrimaryKeyConstraint("id"),

        # Prevent duplicate detection records for the same
        # company, signal type and detection timestamp.
        sa.UniqueConstraint(
            "company_id",
            "signal_type",
            "detected_at",
            name="uq_buying_signals_company_type_time",
        ),
    )

    # ------------------------------------------------------------------
    # Indexes
    # ------------------------------------------------------------------
    op.create_index(
        "ix_buying_signals_company_id",
        "buying_signals",
        ["company_id"],
    )

    op.create_index(
        "ix_buying_signals_category",
        "buying_signals",
        ["signal_category"],
    )

    op.create_index(
        "ix_buying_signals_detected_at",
        "buying_signals",
        ["detected_at"],
    )


def downgrade() -> None:
    """
    Remove the buying_signals table and the SignalStrength enum.

    confidencelevel and sourcetype are shared enums created by
    earlier migrations, so they must NOT be dropped here.
    """

    # ------------------------------------------------------------------
    # Drop indexes
    # ------------------------------------------------------------------
    op.drop_index(
        "ix_buying_signals_detected_at",
        table_name="buying_signals",
    )

    op.drop_index(
        "ix_buying_signals_category",
        table_name="buying_signals",
    )

    op.drop_index(
        "ix_buying_signals_company_id",
        table_name="buying_signals",
    )

    # ------------------------------------------------------------------
    # Drop table
    # ------------------------------------------------------------------
    op.drop_table("buying_signals")

    # ------------------------------------------------------------------
    # Drop only the enum introduced by this migration.
    #
    # Do NOT drop:
    # - confidencelevel
    # - sourcetype
    # ------------------------------------------------------------------
    signalstrength = postgresql.ENUM(
        "LOW",
        "MEDIUM",
        "HIGH",
        name="signalstrength",
    )

    signalstrength.drop(
        op.get_bind(),
        checkfirst=True,
    )