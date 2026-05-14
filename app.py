"""
app.py — Flask backend cho website Hà Nguyễn / Manulife.

Tính năng:
  • Serve index.html + /assets/* (landing page)
  • /thanh-toan          — form chọn sản phẩm + nhập thông tin
  • /thanh-toan/<code>   — màn QR + chờ thanh toán (auto polling)
  • /admin               — admin panel 3 tab (sản phẩm / khách / đơn)
  • /api/orders          — tạo đơn (POST)
  • /api/orders/<code>   — check trạng thái đơn (GET) + sync với Sepay
  • /api/sepay-webhook   — nhận webhook Sepay khi có giao dịch
  • /api/admin/...       — CRUD cho admin panel
"""
import os
import re
import sqlite3
import secrets
import string
import json
from datetime import datetime
from urllib.parse import quote
from flask import (
    Flask, request, jsonify, render_template, send_from_directory,
    abort, redirect, url_for
)

# Cấu hình Sepay (đọc từ env, có default cho user hiện tại)
SEPAY_BANK = os.environ.get('SEPAY_BANK', 'MB')                 # mã ngân hàng (MB cho MBBank)
SEPAY_ACC  = os.environ.get('SEPAY_ACC',  '86868665555')
SEPAY_HOLDER = os.environ.get('SEPAY_HOLDER', 'NGUYEN TRIEU CUONG')
SEPAY_API_TOKEN = os.environ.get('SEPAY_API_TOKEN', '')         # tuỳ chọn, dùng cho polling
WEBHOOK_API_KEY = os.environ.get('SEPAY_WEBHOOK_API_KEY', '')   # khớp với cấu hình Webhook trên dashboard
ORDER_PREFIX = os.environ.get('ORDER_PREFIX', 'HN')             # prefix cho mã đơn

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'brain.db')

app = Flask(__name__, template_folder='templates', static_folder=None)


# -------------------- DB helpers --------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def gen_order_code():
    # HN + 6 chữ số ngẫu nhiên — đủ ngắn để khách gõ tay nếu cần
    suffix = ''.join(secrets.choice(string.digits) for _ in range(6))
    return f'{ORDER_PREFIX}{suffix}'


def build_qr_url(amount: int, code: str) -> str:
    """Tạo URL ảnh QR Sepay (VietQR động)."""
    base = 'https://qr.sepay.vn/img'
    qs = f'acc={quote(SEPAY_ACC)}&bank={quote(SEPAY_BANK)}&amount={int(amount)}&des={quote(code)}'
    return f'{base}?{qs}'


# -------------------- Serve landing + static --------------------
@app.route('/')
def home():
    return send_from_directory(BASE_DIR, 'index.html')


@app.route('/assets/<path:filename>')
def assets(filename):
    return send_from_directory(os.path.join(BASE_DIR, 'assets'), filename)


@app.route('/downloads/<path:filename>')
def downloads(filename):
    folder = os.path.join(BASE_DIR, 'downloads')
    os.makedirs(folder, exist_ok=True)
    return send_from_directory(folder, filename, as_attachment=True)


# -------------------- Checkout flow --------------------
@app.route('/thanh-toan')
def checkout_page():
    conn = db()
    products = conn.execute(
        'SELECT * FROM products WHERE is_active=1 AND stock>0 ORDER BY id DESC'
    ).fetchall()
    conn.close()
    return render_template('checkout.html',
                           products=[dict(p) for p in products])


@app.route('/thanh-toan/<order_code>')
def payment_page(order_code):
    conn = db()
    order = conn.execute(
        'SELECT o.*, p.name AS product_name, p.file_url AS product_file '
        'FROM orders o LEFT JOIN products p ON p.id=o.product_id '
        'WHERE o.order_code=?', (order_code,)
    ).fetchone()
    conn.close()
    if not order:
        abort(404)
    order = dict(order)
    qr_url = build_qr_url(order['amount'], order['order_code'])
    return render_template(
        'payment.html',
        order=order, qr_url=qr_url,
        bank=SEPAY_BANK, acc=SEPAY_ACC, holder=SEPAY_HOLDER
    )


@app.route('/api/orders', methods=['POST'])
def create_order():
    data = request.get_json(silent=True) or request.form.to_dict()
    name  = (data.get('name')  or '').strip()
    phone = (data.get('phone') or '').strip()
    email = (data.get('email') or '').strip()
    product_id = data.get('product_id')

    if not name or not phone:
        return jsonify({'ok': False, 'error': 'Thiếu họ tên hoặc số điện thoại'}), 400
    try:
        product_id = int(product_id)
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'Sản phẩm không hợp lệ'}), 400

    conn = db()
    prod = conn.execute(
        'SELECT * FROM products WHERE id=? AND is_active=1', (product_id,)
    ).fetchone()
    if not prod:
        conn.close()
        return jsonify({'ok': False, 'error': 'Sản phẩm không còn'}), 400
    if (prod['stock'] or 0) <= 0:
        conn.close()
        return jsonify({'ok': False, 'error': 'Sản phẩm hết hàng'}), 400

    # Tạo / cập nhật customer (UNIQUE trên phone+email)
    try:
        conn.execute('''INSERT INTO customers (name, phone, email, zalo, source)
                        VALUES (?,?,?,?,?)''',
                     (name, phone, email or None, phone, 'checkout'))
    except sqlite3.IntegrityError:
        pass
    cust = conn.execute(
        'SELECT id FROM customers WHERE phone=? OR email=? ORDER BY id DESC LIMIT 1',
        (phone, email or '__none__')
    ).fetchone()

    # Tạo order code unique
    for _ in range(8):
        code = gen_order_code()
        exists = conn.execute(
            'SELECT 1 FROM orders WHERE order_code=?', (code,)
        ).fetchone()
        if not exists:
            break
    else:
        conn.close()
        return jsonify({'ok': False, 'error': 'Không sinh được mã đơn'}), 500

    conn.execute('''INSERT INTO orders
        (order_code, customer_id, product_id, customer_name,
         customer_phone, customer_email, amount, status)
        VALUES (?,?,?,?,?,?,?, 'pending')''',
        (code, cust['id'] if cust else None, prod['id'],
         name, phone, email or None, prod['price'])
    )
    conn.commit()
    conn.close()

    return jsonify({
        'ok': True,
        'order_code': code,
        'redirect_url': f'/thanh-toan/{code}'
    })


@app.route('/api/orders/<order_code>/status')
def order_status(order_code):
    conn = db()
    o = conn.execute(
        'SELECT order_code, status, amount, paid_at FROM orders WHERE order_code=?',
        (order_code,)
    ).fetchone()
    conn.close()
    if not o:
        return jsonify({'ok': False, 'error': 'not_found'}), 404
    return jsonify({'ok': True, **dict(o)})


# -------------------- Sepay Webhook --------------------
@app.route('/api/sepay-webhook', methods=['POST'])
def sepay_webhook():
    # Auth check (nếu user đã cấu hình API Key trên dashboard Sepay)
    if WEBHOOK_API_KEY:
        hdr = request.headers.get('Authorization', '')
        if hdr != f'Apikey {WEBHOOK_API_KEY}':
            return jsonify({'success': False, 'message': 'Unauthorized'}), 401

    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({'success': False, 'message': 'No data'}), 400

    gateway       = data.get('gateway')
    tx_date       = data.get('transactionDate')
    account       = data.get('accountNumber')
    sub_account   = data.get('subAccount')
    transfer_type = (data.get('transferType') or '').lower()
    amount        = int(data.get('transferAmount') or 0)
    accumulated   = int(data.get('accumulated') or 0)
    code          = data.get('code')
    content       = data.get('content') or ''
    ref_number    = data.get('referenceCode')
    body          = data.get('description') or ''
    sepay_id      = str(data.get('id') or '')

    amount_in  = amount if transfer_type == 'in'  else 0
    amount_out = amount if transfer_type == 'out' else 0

    # Tìm mã đơn trong content (Sepay đôi khi đặt sẵn 'code', đôi khi chỉ có trong content)
    candidate = (code or '').strip()
    if not candidate:
        m = re.search(rf'\b({ORDER_PREFIX}\d{{4,}})\b', content.upper())
        if m:
            candidate = m.group(1)

    conn = db()
    matched_order_id = None
    if candidate and transfer_type == 'in':
        row = conn.execute(
            'SELECT id, amount, status FROM orders WHERE order_code=?',
            (candidate.upper(),)
        ).fetchone()
        if row and row['status'] == 'pending':
            # Chỉ mark success nếu đủ tiền
            if amount >= int(row['amount']):
                conn.execute(
                    '''UPDATE orders SET status='success',
                       paid_at=CURRENT_TIMESTAMP, payment_ref=? WHERE id=?''',
                    (ref_number or sepay_id, row['id'])
                )
                # Trừ tồn kho
                conn.execute('''UPDATE products SET stock = MAX(stock-1, 0)
                                WHERE id=(SELECT product_id FROM orders WHERE id=?)''',
                             (row['id'],))
                matched_order_id = row['id']
            else:
                conn.execute(
                    '''UPDATE orders SET notes = COALESCE(notes,'') ||
                       ?, payment_ref=? WHERE id=?''',
                    (f' [Thiếu {row["amount"]-amount}đ] ', ref_number or sepay_id, row['id'])
                )

    conn.execute('''INSERT INTO sepay_transactions
        (sepay_id, gateway, transaction_date, account_number,
         amount_in, amount_out, accumulated, code, content,
         reference_number, body, matched_order_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
        (sepay_id, gateway, tx_date, account, amount_in, amount_out,
         accumulated, candidate or None, content, ref_number, body,
         matched_order_id)
    )
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'matched': bool(matched_order_id)})


# -------------------- Admin Panel --------------------
@app.route('/admin')
def admin_page():
    return render_template('admin.html',
                           bank=SEPAY_BANK, acc=SEPAY_ACC, holder=SEPAY_HOLDER)


# ---- Products CRUD ----
@app.route('/api/admin/products', methods=['GET', 'POST'])
def api_products():
    conn = db()
    if request.method == 'GET':
        rows = conn.execute('SELECT * FROM products ORDER BY id DESC').fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])

    d = request.get_json(force=True)
    conn.execute('''INSERT INTO products (name, price, description, file_url, stock, is_active)
                    VALUES (?,?,?,?,?,?)''',
                 (d.get('name'), int(d.get('price') or 0),
                  d.get('description'), d.get('file_url'),
                  int(d.get('stock') or 999),
                  1 if d.get('is_active', True) else 0))
    conn.commit(); conn.close()
    return jsonify({'ok': True})


@app.route('/api/admin/products/<int:pid>', methods=['PUT', 'DELETE'])
def api_product_one(pid):
    conn = db()
    if request.method == 'DELETE':
        conn.execute('DELETE FROM products WHERE id=?', (pid,))
        conn.commit(); conn.close()
        return jsonify({'ok': True})

    d = request.get_json(force=True)
    conn.execute('''UPDATE products SET name=?, price=?, description=?,
                    file_url=?, stock=?, is_active=? WHERE id=?''',
                 (d.get('name'), int(d.get('price') or 0),
                  d.get('description'), d.get('file_url'),
                  int(d.get('stock') or 0),
                  1 if d.get('is_active', True) else 0, pid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})


# ---- Customers CRUD ----
@app.route('/api/admin/customers', methods=['GET', 'POST'])
def api_customers():
    conn = db()
    if request.method == 'GET':
        rows = conn.execute('SELECT * FROM customers ORDER BY id DESC').fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])

    d = request.get_json(force=True)
    try:
        conn.execute('''INSERT INTO customers (name, phone, email, zalo, source, notes)
                        VALUES (?,?,?,?,?,?)''',
                     (d.get('name'), d.get('phone'), d.get('email'),
                      d.get('zalo'), d.get('source', 'manual'), d.get('notes')))
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.close()
        return jsonify({'ok': False, 'error': str(e)}), 400
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/admin/customers/<int:cid>', methods=['PUT', 'DELETE'])
def api_customer_one(cid):
    conn = db()
    if request.method == 'DELETE':
        conn.execute('DELETE FROM customers WHERE id=?', (cid,))
        conn.commit(); conn.close()
        return jsonify({'ok': True})
    d = request.get_json(force=True)
    conn.execute('''UPDATE customers SET name=?, phone=?, email=?, zalo=?, notes=?
                    WHERE id=?''',
                 (d.get('name'), d.get('phone'), d.get('email'),
                  d.get('zalo'), d.get('notes'), cid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})


# ---- Orders CRUD ----
@app.route('/api/admin/orders', methods=['GET', 'POST'])
def api_orders():
    conn = db()
    if request.method == 'GET':
        rows = conn.execute('''
            SELECT o.*, p.name AS product_name
            FROM orders o LEFT JOIN products p ON p.id=o.product_id
            ORDER BY o.id DESC''').fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])

    d = request.get_json(force=True)
    product_id = int(d.get('product_id'))
    prod = conn.execute('SELECT * FROM products WHERE id=?', (product_id,)).fetchone()
    if not prod:
        conn.close()
        return jsonify({'ok': False, 'error': 'Sản phẩm không tồn tại'}), 400
    amount = int(d.get('amount') or prod['price'])

    # Tạo/khớp customer
    name = d.get('customer_name') or ''
    phone = d.get('customer_phone') or ''
    email = d.get('customer_email') or ''
    cust_id = None
    if phone or email:
        try:
            conn.execute('''INSERT INTO customers (name, phone, email, zalo, source)
                            VALUES (?,?,?,?,?)''', (name, phone, email, phone, 'admin'))
        except sqlite3.IntegrityError:
            pass
        c = conn.execute('SELECT id FROM customers WHERE phone=? OR email=? ORDER BY id DESC LIMIT 1',
                         (phone, email or '__none__')).fetchone()
        if c: cust_id = c['id']

    # Sinh mã đơn unique
    for _ in range(8):
        code = gen_order_code()
        if not conn.execute('SELECT 1 FROM orders WHERE order_code=?', (code,)).fetchone():
            break

    status = d.get('status', 'pending')
    conn.execute('''INSERT INTO orders
        (order_code, customer_id, product_id, customer_name, customer_phone,
         customer_email, amount, status, paid_at)
        VALUES (?,?,?,?,?,?,?,?, CASE WHEN ?='success' THEN CURRENT_TIMESTAMP ELSE NULL END)''',
        (code, cust_id, product_id, name, phone, email, amount, status, status))
    if status == 'success':
        conn.execute('UPDATE products SET stock = MAX(stock-1, 0) WHERE id=?', (product_id,))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'order_code': code})


@app.route('/api/admin/orders/<int:oid>', methods=['PUT', 'DELETE'])
def api_order_one(oid):
    conn = db()
    if request.method == 'DELETE':
        conn.execute('DELETE FROM orders WHERE id=?', (oid,))
        conn.commit(); conn.close()
        return jsonify({'ok': True})

    d = request.get_json(force=True)
    cur = conn.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone()
    if not cur:
        conn.close(); return jsonify({'ok': False, 'error': 'not_found'}), 404
    new_status = d.get('status', cur['status'])
    # Nếu chuyển sang success → set paid_at + trừ kho
    if cur['status'] != 'success' and new_status == 'success':
        conn.execute('''UPDATE orders SET status='success',
                        paid_at=CURRENT_TIMESTAMP, notes=? WHERE id=?''',
                     (d.get('notes', cur['notes']) or '[manual]', oid))
        conn.execute('UPDATE products SET stock = MAX(stock-1, 0) WHERE id=?',
                     (cur['product_id'],))
    else:
        conn.execute('UPDATE orders SET status=?, notes=? WHERE id=?',
                     (new_status, d.get('notes', cur['notes']), oid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})


# Health check
@app.route('/api/health')
def health():
    return jsonify({'ok': True, 'time': datetime.utcnow().isoformat()})


if __name__ == '__main__':
    # Đảm bảo DB đã được init
    if not os.path.exists(DB_PATH):
        from init_db import setup_database
        setup_database()
    port = int(os.environ.get('PORT', 5050))
    debug = os.environ.get('FLASK_DEBUG', '0') == '1'
    app.run(host='0.0.0.0', port=port, debug=debug)
