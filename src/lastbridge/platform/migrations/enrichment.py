SCHEMA = """CREATE TABLE IF NOT EXISTS media_enrichment (
    source TEXT NOT NULL,
    policy TEXT NOT NULL,
    document TEXT NOT NULL,
    updated REAL NOT NULL,
    PRIMARY KEY(source,policy),
    FOREIGN KEY(source,policy) REFERENCES media_files(source,policy)
        ON DELETE CASCADE
)"""
