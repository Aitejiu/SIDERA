"""Create the independent R5.3 Knowledge foundation schema."""

from alembic import op

from sidera_knowledge.persistence.models import Base

revision = "0001_knowledge_foundation"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The metadata is the single schema contract; SQLAlchemy emits the circular
    # current-revision foreign key as a deferred ALTER after its target exists.
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=False)


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind(), checkfirst=True)
