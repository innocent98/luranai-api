"""Delete all flow templates to allow re-seeding with fixes."""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from app.db.session import SessionLocal
from app.db.models.flow import FlowTemplate
from sqlalchemy import delete

def delete_all_templates():
    """Delete all flow templates from database."""
    db = SessionLocal()

    try:
        # Delete all flow templates
        stmt = delete(FlowTemplate)
        result = db.execute(stmt)
        db.commit()

        print(f"✅ Deleted {result.rowcount} flow templates")
        print("Now run: docker-compose exec api python scripts/seed_flow_templates.py")

    except Exception as e:
        db.rollback()
        print(f"❌ Error deleting templates: {e}")
        raise
    finally:
        db.close()

if __name__ == "__main__":
    delete_all_templates()
