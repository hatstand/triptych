-- migrate:up
SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- Ferries are a reservation kind. The new list only adds to the old one, so nothing that held
-- before stops holding, and validating it scans one small table, so its brief lock is fine.
-- squawk-ignore ban-drop-constraint
ALTER TABLE reservation DROP CONSTRAINT reservation_kind_check;
ALTER TABLE reservation ADD CONSTRAINT reservation_kind_check
    CHECK (kind IN ('flight', 'lodging', 'car', 'rail', 'ferry', 'restaurant', 'activity', 'other')) NOT VALID;
-- squawk-ignore constraint-missing-not-valid
ALTER TABLE reservation VALIDATE CONSTRAINT reservation_kind_check;

-- migrate:down
UPDATE reservation SET kind = 'other' WHERE kind = 'ferry';
ALTER TABLE reservation DROP CONSTRAINT reservation_kind_check;
ALTER TABLE reservation ADD CONSTRAINT reservation_kind_check
    CHECK (kind IN ('flight', 'lodging', 'car', 'rail', 'restaurant', 'activity', 'other'));
