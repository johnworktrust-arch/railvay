ALTER TABLE generations
    ADD CONSTRAINT generations_no_music_before_002
    CHECK (generation_type <> 'music');

DELETE FROM model_prices mp
WHERE mp.generation_type = 'music'
  AND NOT EXISTS (
      SELECT 1 FROM generations g WHERE g.model_price_id = mp.id
  );

ALTER TABLE model_prices
    ADD CONSTRAINT model_prices_no_music_before_002
    CHECK (generation_type <> 'music');

ALTER TABLE model_prices
    DROP CONSTRAINT IF EXISTS model_prices_generation_type_check;

ALTER TABLE generations
    DROP CONSTRAINT IF EXISTS generations_generation_type_check;

ALTER TABLE model_prices
    ADD CONSTRAINT model_prices_generation_type_check
    CHECK (generation_type IN ('text', 'image', 'video', 'tts', 'seo'));

ALTER TABLE generations
    ADD CONSTRAINT generations_generation_type_check
    CHECK (generation_type IN ('text', 'image', 'video', 'tts', 'seo'));

ALTER TABLE model_prices
    DROP CONSTRAINT IF EXISTS model_prices_no_music_before_002;

ALTER TABLE generations
    DROP CONSTRAINT IF EXISTS generations_no_music_before_002;
