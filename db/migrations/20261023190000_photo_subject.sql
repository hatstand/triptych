-- migrate:up
SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- Where the photo's main subject is, so pages crop around it rather than the middle (a skyline
-- loses its towers to a centre crop). Found by the model once per photo: `subject` is the box
-- [left, top, right, bottom] as fractions of the image, null if it found none; `aspect` is the
-- image's width / height, which the crop needs. Unchecked photos are cropped with a default.
ALTER TABLE place_photo ADD COLUMN IF NOT EXISTS aspect real;
ALTER TABLE place_photo ADD COLUMN IF NOT EXISTS subject real[];
ALTER TABLE place_photo ADD COLUMN IF NOT EXISTS subject_checked boolean NOT NULL DEFAULT false;

-- migrate:down
-- squawk-ignore ban-drop-column
ALTER TABLE place_photo DROP COLUMN IF EXISTS subject_checked;
-- squawk-ignore ban-drop-column
ALTER TABLE place_photo DROP COLUMN IF EXISTS subject;
-- squawk-ignore ban-drop-column
ALTER TABLE place_photo DROP COLUMN IF EXISTS aspect;
