CREATE TABLE rules (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    trigger TEXT NOT NULL,
    schedule JSONB,
    condition_text TEXT,
    condition_amount NUMERIC(12, 2),
    action TEXT NOT NULL,
    params JSONB NOT NULL DEFAULT '{}',
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    last_fired_on DATE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_rules_user_trigger ON rules (user_id, trigger);

CREATE TABLE rule_runs (
    id BIGSERIAL PRIMARY KEY,
    rule_id BIGINT NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ok BOOLEAN NOT NULL,
    message TEXT NOT NULL
);

CREATE INDEX idx_rule_runs_user ON rule_runs (user_id, id);
