-- migrate:up
SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- Trains have no airport codes, so a departure zone that differs from the arrival one was guessed
-- from wherever the previous leg landed, however long before (a Chengdu train a week after landing
-- in Hong Kong got Asia/Hong_Kong). Clear those guesses; the worker refills them on start
-- (db.refill_timezones) with the rule that only trusts a landing from the day before.
UPDATE reservation SET start_tz = NULL WHERE kind = 'rail' AND start_tz IS DISTINCT FROM end_tz;

-- migrate:down
