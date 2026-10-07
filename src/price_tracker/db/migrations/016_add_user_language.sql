-- 016 (SP1). Additive only. No comment in this file contains a semicolon.
-- users.language: interface language chosen explicitly (Settings -> Language).
-- NULL = Automatic. Values are gettext locale codes validated in code, not by CHECK,
-- so adding a catalogue later stays an additive change.
ALTER TABLE users ADD COLUMN language TEXT;
-- users.telegram_language_tag: last IETF tag observed on an Update from this user
-- (User.language_code), stored verbatim, at most 35 characters (enforced in code).
-- NULL until first observed. It is an observation, never a choice.
ALTER TABLE users ADD COLUMN telegram_language_tag TEXT;
