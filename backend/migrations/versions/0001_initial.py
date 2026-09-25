"""Create initial lead intelligence schema."""
from alembic import op
from app.models import Base

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    """Create all initial tables and indexes from model metadata."""
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)

def downgrade():
    """Drop application tables."""
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
