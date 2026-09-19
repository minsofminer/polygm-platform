-- P11 D3 · Following a trader.
--
-- A follow is a decision about somebody else's identity, and the identity this product publishes is the
-- pseudonym — so the follow is keyed by the pseudonym and never by an address. That has a consequence worth
-- stating: two accounts following the same trader are two rows about one published identity, and the table can
-- be read without ever resolving anybody's address.
--
-- What a follow is NOT: a copy config. `copy_configs` (0004/0011) is the thing that can place orders, with its
-- own dry-run history and its own guards. A follow is a watch — it feeds a list and it changes nothing about
-- what gets traded, which is why it needs no notional, no slippage warning and no second confirmation.
--
-- `label` is the trader's own name at the moment the follow was made, kept as a convenience for the list and
-- never as a source of truth: the row's identity is `anon_wallet`, and a rename elsewhere must not silently
-- point this row at a different person.
CREATE TABLE IF NOT EXISTS trader_follows (
    user_id     TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    anon_wallet TEXT NOT NULL,
    label       TEXT NOT NULL DEFAULT '',
    created_ms  BIGINT NOT NULL,
    -- One follow per (account, trader). The primary key is the whole constraint: following twice is one
    -- relationship, and a second row would only make the count wrong.
    PRIMARY KEY (user_id, anon_wallet)
);
-- Read path 1: the follows list asks "this account's follows, newest first" — a prefix scan on user_id, ordered
-- by created_ms, so the list never sorts a table and never scans another account's rows.
CREATE INDEX trader_follows_user_ix ON trader_follows (user_id, created_ms DESC);
-- Read path 2: "does this account follow this wallet" is answered by the primary key alone (exact lookup), and
-- the reverse question the anti-gaming work will ask — "who follows this trader, and how many" — is a range scan
-- on anon_wallet. Without this index that question is a full table scan of every follow in the product.
CREATE INDEX trader_follows_wallet_ix ON trader_follows (anon_wallet, created_ms DESC);
