SCHEMA = """CREATE TABLE media_files (
    source TEXT NOT NULL,
    policy TEXT NOT NULL CHECK(policy IN ('aac','opus')),
    relative_path TEXT,
    container TEXT NOT NULL,
    size INTEGER,
    checksum TEXT,
    provider_identity TEXT,
    state TEXT NOT NULL CHECK(state IN
        ('pending','writing','ready','missing','deleting')),
    created REAL NOT NULL,
    updated REAL NOT NULL,
    PRIMARY KEY(source,policy)
)"""
