"""
init_db.py — Khởi tạo brain.db cho hệ thống bán hàng + CRM
Chạy 1 lần: python3 init_db.py
Chạy lại: KHÔNG xóa data của products/customers/orders, chỉ tạo table nếu chưa có.
"""
import sqlite3
import json
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), 'brain.db')


def setup_database():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # === 3 bảng gốc từ brain.db.py (knowledge / business / brand_voice) ===
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS knowledge (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS business (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS brand_voice (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    ''')

    # === 3 bảng mới cho CRM + bán hàng ===
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        price INTEGER NOT NULL,                -- VND, nguyên đồng
        description TEXT,
        file_url TEXT,                          -- link file PDF/ebook giao cho khách
        stock INTEGER DEFAULT 999,
        is_active INTEGER DEFAULT 1,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    ''')

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS customers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        phone TEXT,
        email TEXT,
        zalo TEXT,
        source TEXT,                            -- waitlist / checkout / manual ...
        notes TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(phone, email)
    )
    ''')

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_code TEXT UNIQUE NOT NULL,        -- vd HN1747200001  (nội dung CK)
        customer_id INTEGER,
        product_id INTEGER,
        customer_name TEXT,
        customer_phone TEXT,
        customer_email TEXT,
        amount INTEGER NOT NULL,                -- VND
        status TEXT DEFAULT 'pending',          -- pending | success | failed | cancelled
        payment_ref TEXT,                       -- transactionDate/referenceCode từ Sepay
        notes TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        paid_at DATETIME,
        FOREIGN KEY(customer_id) REFERENCES customers(id),
        FOREIGN KEY(product_id) REFERENCES products(id)
    )
    ''')

    # Log mọi giao dịch Sepay đã nhận (dù match đơn hay không)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS sepay_transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sepay_id TEXT,
        gateway TEXT,
        transaction_date TEXT,
        account_number TEXT,
        amount_in INTEGER DEFAULT 0,
        amount_out INTEGER DEFAULT 0,
        accumulated INTEGER DEFAULT 0,
        code TEXT,
        content TEXT,
        reference_number TEXT,
        body TEXT,
        matched_order_id INTEGER,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    ''')

    # === Seed brand_voice nếu chưa có (giữ logic gốc) ===
    cursor.execute('SELECT COUNT(*) FROM brand_voice')
    if cursor.fetchone()[0] == 0:
        brand_voice_data = [
            ('Quy tắc Tone & Style', '''Tone của tôi: Storytelling, gần gũi, thẳng thắn, không dùng từ hoa mỹ, không giống AI.
Tôi hay dùng những từ như: không cần phức tạp, quy trình, tài chính, hiểu.
Tôi không bao giờ dùng: tối ưu hóa trải nghiệm, cam kết, từ quá corporate.
Từ bị cấm bổ sung: có vẻ, nhìn có vẻ, hãy cùng, chặng đường, hành trình, vun đắp, lan tỏa, điều đó, thắp sáng, để từ đó, từ đó, tựu trung, không chỉ... mà còn, quan trọng hơn hết, điều quan trọng là, suy cho cùng.'''),
            ('Đối tượng độc giả mục tiêu', 'Người trẻ đã có gia đình, thu nhập trên 15tr/tháng, quan tâm tài chính.'),
        ]
        cursor.executemany('INSERT INTO brand_voice (title, content) VALUES (?, ?)', brand_voice_data)

    # === Seed knowledge / business nếu chưa có ===
    cursor.execute('SELECT COUNT(*) FROM business')
    if cursor.fetchone()[0] == 0:
        cursor.execute(
            "INSERT INTO business (title, content) VALUES (?, ?)",
            ('Profile Hà Nguyễn',
             'Tư vấn tài chính Manulife — 7 năm kinh nghiệm — MDRT/COT — 500+ gia đình.')
        )

    # === Seed 1 sản phẩm ebook bảo hiểm để có data demo ===
    cursor.execute('SELECT COUNT(*) FROM products')
    if cursor.fetchone()[0] == 0:
        cursor.execute('''INSERT INTO products
            (name, price, description, file_url, stock, is_active)
            VALUES (?, ?, ?, ?, ?, ?)''',
            ('Ebook: 10 câu hỏi phải biết trước khi mua bảo hiểm nhân thọ',
             49000,
             'Bộ 10 câu hỏi tỉnh táo giúp bạn không bị "đẹp đẽ phô trương" khi nghe tư vấn bảo hiểm. PDF 20 trang, đọc trong 30 phút. Tặng kèm 1 buổi tư vấn miễn phí 15 phút với Hà.',
             '/downloads/ebook-bao-hiem-10-cau-hoi.pdf',
             999, 1)
        )

    # === Import waitlist.json (nếu có) vào customers ===
    waitlist_path = os.path.join(os.path.dirname(__file__), 'waitlist.json')
    imported = 0
    if os.path.exists(waitlist_path):
        try:
            with open(waitlist_path, 'r', encoding='utf-8') as f:
                rows = json.load(f)
            if isinstance(rows, dict):
                rows = rows.get('items') or rows.get('data') or []
            for r in rows or []:
                name = r.get('name') or r.get('full_name') or r.get('ho_ten') or 'Không tên'
                phone = r.get('phone') or r.get('sdt') or r.get('mobile')
                email = r.get('email')
                zalo = r.get('zalo') or phone
                try:
                    cursor.execute('''INSERT OR IGNORE INTO customers
                        (name, phone, email, zalo, source) VALUES (?,?,?,?,?)''',
                        (name, phone, email, zalo, 'waitlist'))
                    if cursor.rowcount:
                        imported += 1
                except Exception:
                    pass
        except Exception as e:
            print(f'Warning waitlist.json: {e}')

    conn.commit()
    conn.close()
    print(f'✅ brain.db ready at {DB_PATH}')
    if imported:
        print(f'   → Imported {imported} customers from waitlist.json')


if __name__ == '__main__':
    setup_database()
