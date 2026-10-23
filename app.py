"""Local personal finance application."""
import hmac
import importlib
import os
from datetime import datetime
from pathlib import Path
from typing import Any
import streamlit as st
from core.db import get_conn, init_db, backup_db, is_postgres

st.set_page_config(page_title='Миний санхүү', layout='wide')

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
DB_FILE = APP_DIR / 'finance.db'
BACKUP_DIR = APP_DIR / 'backups'


@st.cache_resource
def connection() -> Any:
    """Initialize one shared local database connection."""
    conn = get_conn(str(DB_FILE))
    init_db(conn)
    return conn


conn = connection()
count = conn.execute(
    """SELECT COUNT(*) FROM transactions AS t
       LEFT JOIN categories AS c ON c.id=t.category_id
       WHERE t.category_id IS NULL AND t.classified_by='none'
         AND (c.type IS NULL OR c.type!='fee')"""
).fetchone()[0]
pages = ['Upload', 'Review', 'Transactions', 'Rules', 'Categories', 'Reports', 'AI_Analysis']
page_labels = {
    'Upload': 'Хуулга оруулах',
    'Review': 'Ангилах',
    'Transactions': 'Гүйлгээнүүд',
    'Rules': 'Дүрмүүд',
    'Categories': 'Ангиллууд',
    'Reports': 'Тайлан',
    'AI_Analysis': 'AI шинжилгээ',
}
if 'next_page' in st.session_state:
    st.session_state['page'] = st.session_state.pop('next_page')
page = st.sidebar.radio(
    'Миний санхүү',
    pages,
    key='page',
    format_func=lambda selected: (
        f"Ангилах ({count})"
        if selected == 'Review'
        else page_labels[selected]
    ),
)
if count:
    st.sidebar.markdown(f':red[Ангилаагүй {count} гүйлгээ байна]')
if st.sidebar.button('Гарах'):
    st.session_state['authenticated'] = False
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
        conn.rollback()
        st.error('Үйлдлийг гүйцэтгэхэд алдаа гарлаа.')
        with st.expander('Алдааны техникийн дэлгэрэнгүй'):
            st.exception(exc)
db_file = DB_FILE
database_counts = conn.execute(
    'SELECT (SELECT COUNT(*) FROM transactions), '
    '(SELECT COUNT(*) FROM imports)'
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
conn.commit()
