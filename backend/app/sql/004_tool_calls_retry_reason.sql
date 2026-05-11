-- E2: add retry_reason on tool_calls so every row with retry_number > 0
-- carries an explanation of the prior attempt's outcome.
ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS retry_reason TEXT;
