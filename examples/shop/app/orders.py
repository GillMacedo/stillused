# Made-up code for the demo.
def order_total(db, order_id):
    return db.fetch("SELECT total, discount_code FROM orders WHERE id = %s", order_id)


def apply_coupon(db, code):
    return db.fetch("SELECT * FROM coupons WHERE code = %s", code)
