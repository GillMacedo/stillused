-- Made-up migration for the demo.
ALTER TABLE orders DROP COLUMN discount_code;
ALTER TABLE orders RENAME COLUMN total TO total_cents;
ALTER TABLE customers DROP COLUMN legacy_ref;
DROP TABLE IF EXISTS coupons;
