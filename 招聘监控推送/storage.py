# ============================================================
# 存储模块 — SQLite 数据库操作
# 记录已推送的条目 ID，确保去重
# ============================================================
import sqlite3
import os
from datetime import datetime

try:
    from config import DB_PATH as _DEFAULT_DB
except ImportError:      # 独立使用本模块时的兜底
    _DEFAULT_DB = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "job_monitor.db"
    )


class JobStorage:
    def __init__(self, db_path=None):
        self.db_path = db_path or _DEFAULT_DB
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self._init_db()

    def _init_db(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS pushed_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id TEXT NOT NULL UNIQUE,
                category TEXT NOT NULL,
                title TEXT,
                pushed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS keywords (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT NOT NULL UNIQUE,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.conn.commit()

    def is_pushed(self, item_id):
        """检查某条信息是否已推送过"""
        cursor = self.conn.execute(
            "SELECT 1 FROM pushed_items WHERE item_id = ?", (item_id,)
        )
        return cursor.fetchone() is not None

    def mark_pushed(self, item_id, category, title=""):
        """标记为已推送"""
        try:
            self.conn.execute(
                "INSERT INTO pushed_items (item_id, category, title) VALUES (?, ?, ?)",
                (item_id, category, title),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False  # 已存在，跳过

    def get_new_items(self, items, category, id_field="item_id"):
        """过滤出未推送的新条目"""
        new_items = []
        for item in items:
            item_id = item.get(id_field, "")
            if not item_id:
                continue
            if not self.is_pushed(item_id):
                new_items.append(item)
        return new_items

    def get_stats(self):
        """获取统计信息"""
        total = self.conn.execute(
            "SELECT COUNT(*) FROM pushed_items"
        ).fetchone()[0]
        today = self.conn.execute(
            "SELECT COUNT(*) FROM pushed_items WHERE date(pushed_at) = date('now', 'localtime')"
        ).fetchone()[0]
        return {"total": total, "today": today}

    def close(self):
        self.conn.close()
