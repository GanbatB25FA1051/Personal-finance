"""Local personal finance application."""
import hmac
import importlib
import os
from datetime import datetime
from pathlib import Path
import streamlit as st
from core.auth import clear_session_state
from core.db import get_conn, init_db, backup_db, is_postgres
from core.errors import log_ui_error
from core.deployment import hosted_database_configured

st.set_page_config(page_title='Миний санхүү', layout='wide')

if not hosted_database_configured(os.environ):
    st.error('Үүлэн өгөгдлийн сан тохируулаагүй байна. Render-ийн тохиргоог шалгана уу.')
    st.stop()

if not st.session_state.get('authenticated', False):
    st.title('Нэвтрэх')
    app_password = os.environ.get('FINANCE_APP_PASSWORD', '')
    if not app_password:
        st.error('Серверийн нууц үг тохируулаагүй байна.')
        st.stop()
    with st.form('login_form'):
        entered_password = st.text_input('Нууц үг', type='password')
        submitted = st.form_submit_button('Нэвтрэх')
    if submitted:
        if hmac.compare_digest(entered_password, app_password):
            st.session_state['authenticated'] = True
            st.rerun()
        st.error('Нууц үг буруу байна.')
    st.stop()

APP_DIR = Path(__file__).resolve().parent
DB_FILE = Path(os.environ.get('FINANCE_DB_PATH', APP_DIR / 'finance.db')).expanduser()
BACKUP_DIR = APP_DIR / 'backups'


conn = get_conn(str(DB_FILE))
try:
    init_db(conn)
    count = conn.execute(
        """SELECT COUNT(*) FROM transactions AS t
           LEFT JOIN categories AS c ON c.id=t.category_id
           WHERE t.category_id IS NULL AND t.classified_by='none'
             AND (c.type IS NULL OR c.type!='fee')"""
    ).fetchone()[0]
    pages = [
        'Home',
        'Upload',
        'Review',
        'Transactions',
        'Rules',
        'Categories',
        'Reports',
        'Budget',
        'Goals',
        'AI_Analysis',
    ]
    page_labels = {
        'Home': ':material/home: Нүүр хуудас',
        'Upload': ':material/upload_file: Хуулга оруулах',
        'Review': ':material/rule: Ангилах',
        'Transactions': ':material/receipt_long: Гүйлгээнүүд',
        'Rules': ':material/tune: Дүрмүүд',
        'Categories': ':material/category: Ангиллууд',
        'Reports': ':material/analytics: Тайлан',
        'Budget': ':material/account_balance_wallet: Төсөв',
        'Goals': ':material/flag: Урт хугацааны зорилго',
        'AI_Analysis': ':material/auto_awesome: AI шинжилгээ',
    }
    if 'next_page' in st.session_state:
        st.session_state['page'] = st.session_state.pop('next_page')
    page = st.sidebar.radio(
        'Миний санхүү',
        pages,
        key='page',
        format_func=lambda selected: (
            f":material/rule: Ангилах ({count})"
            if selected == 'Review'
            else page_labels[selected]
        ),
    )
    if count:
        st.sidebar.markdown(f':red[Ангилаагүй {count} гүйлгээ байна]')
    if st.sidebar.button('Гарах'):
        clear_session_state(st.session_state)
        st.rerun()
    try:
        module = importlib.import_module('ui.' + page.lower())
    except ModuleNotFoundError as exc:
        if exc.name == 'ui.' + page.lower():
            st.info('Энэ хэсгийг хараахан бэлдээгүй байна.')
        else:
            raise
    else:
        try:
            module.render(conn)
        except Exception as exc:
            st.error('Үйлдлийг гүйцэтгэхэд алдаа гарлаа.')
            error_id = log_ui_error(page, exc)
            st.caption(f'Алдааны дугаар: {error_id}. Дахин оролдоно уу.')
    db_file = DB_FILE
    database_counts = conn.execute(
        'SELECT (SELECT COUNT(*) FROM transactions) AS transaction_count, '
        '(SELECT COUNT(*) FROM imports) AS import_count'
    ).fetchone()
    st.sidebar.caption(
        (
            'Өгөгдлийн сан: Neon PostgreSQL · '
            f'{database_counts[0]:,} гүйлгээ · {database_counts[1]:,} хуулга'
        )
        if is_postgres(conn)
        else (
            f"Өгөгдлийн сан: {db_file.stat().st_size / 1024:,.1f} КБ · "
            f"{database_counts[0]:,} гүйлгээ · {database_counts[1]:,} хуулга"
            if db_file.exists()
            else 'Өгөгдлийн сан: хоосон'
        )
    )
    if not is_postgres(conn):
        backups = sorted(BACKUP_DIR.glob('finance_*.db'), key=lambda p: p.stat().st_mtime, reverse=True)
        st.sidebar.caption(
            'Сүүлд нөөцөлсөн: '
            + (
                datetime.fromtimestamp(backups[0].stat().st_mtime).strftime('%Y-%m-%d %H:%M')
                if backups
                else 'хараахан нөөцлөөгүй'
            )
        )
        if st.sidebar.button('Одоо нөөцлөх'):
            target = backup_db(str(DB_FILE), str(BACKUP_DIR))
            st.sidebar.success(
                'Нөөц амжилттай хадгалагдлаа.'
                if target
                else 'Нөөцлөх өгөгдлийн сан алга.'
            )
finally:
    conn.close()
