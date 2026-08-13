CREATE TABLE folders (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    parent_id   TEXT DEFAULT NULL,
    created_at  REAL,
    FOREIGN KEY (parent_id) REFERENCES folders(id) ON DELETE CASCADE
);

CREATE INDEX idx_folder_parent ON folders(parent_id);

CREATE TABLE users (
    id            TEXT PRIMARY KEY,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT DEFAULT 'user',
    created_at    REAL
);

CREATE INDEX idx_users_username ON users(username);

CREATE TABLE user_credentials (
    id        TEXT PRIMARY KEY,
    user_id   TEXT NOT NULL,
    node_name TEXT NOT NULL,
    username  TEXT NOT NULL,
    created_at REAL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    UNIQUE(user_id, node_name)
);

CREATE INDEX idx_uc_user ON user_credentials(user_id);

CREATE TABLE experiments (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    blocks      TEXT DEFAULT '[]',
    tags        TEXT DEFAULT '[]',
    created_by  TEXT DEFAULT '',
    folder_id   TEXT DEFAULT NULL,
    created_at  REAL,
    updated_at  REAL,
    FOREIGN KEY (folder_id) REFERENCES folders(id) ON DELETE SET NULL
);

CREATE INDEX idx_exp_created ON experiments(created_at);
CREATE INDEX idx_exp_created_by ON experiments(created_by);
CREATE INDEX idx_exp_folder ON experiments(folder_id);

