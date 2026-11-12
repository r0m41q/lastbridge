import json


class QueueRepository:
    def __init__(self, database):
        self.database = database

    def get(self, user_id):
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT document FROM play_queues WHERE user_id=?",
                (user_id,),
            ).fetchone()
        return json.loads(row["document"]) if row else None

    def save(self, user_id, document):
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO play_queues(user_id,document) VALUES(?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "document=excluded.document",
                (user_id, json.dumps(document)),
            )
