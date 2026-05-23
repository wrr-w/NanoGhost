import time
import uuid

from agent_core.interfaces import ImagePort


class SqliteImagePort(ImagePort):

    def __init__(self, db):
        self.db = db

    def add_image(self, base64):
        img_id = f"img-{uuid.uuid4()}"
        now = time.time()
        with self.db._conn() as conn:
            conn.execute("INSERT INTO agent_images (id, base64, ref_count, created_at) VALUES (?, ?, 1, ?)",
                         (img_id, base64, now))
            conn.commit()
        return img_id

    def get_image(self, image_id):
        with self.db._conn() as conn:
            row = conn.execute("SELECT id, base64, created_at FROM agent_images WHERE id=?", (image_id,)).fetchone()
            return dict(row) if row else None

    def get_images_batch(self, ids):
        return self.db.get_agent_images_batch(ids)

    def increment_references(self, ids):
        with self.db._conn() as conn:
            for img_id in ids:
                conn.execute("UPDATE agent_images SET ref_count = ref_count + 1 WHERE id=?", (img_id,))
            conn.commit()

    def decrement_references(self, ids):
        deleted = []
        with self.db._conn() as conn:
            for img_id in ids:
                row = conn.execute("SELECT ref_count FROM agent_images WHERE id=?", (img_id,)).fetchone()
                if not row:
                    continue
                if row["ref_count"] <= 1:
                    conn.execute("DELETE FROM agent_images WHERE id=?", (img_id,))
                    deleted.append(img_id)
                else:
                    conn.execute("UPDATE agent_images SET ref_count = ref_count - 1 WHERE id=?", (img_id,))
            conn.commit()
        return deleted
