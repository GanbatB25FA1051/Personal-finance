"""Statement upload and import history."""
import hashlib
import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st

from core.db import backup_db, is_postgres, read_sql_query
from core.ingest import import_statement
from core.models import ImportResult
from ui import translate_import_message

MAX_FILE_SIZE = 20 * 1024 * 1024
APP_DIR = Path(__file__).resolve().parent.parent
STATEMENT_ARCHIVE = APP_DIR / 'imported_statements'


def _import_and_archive(
    conn: sqlite3.Connection,
    data: bytes,
    filename: str,
) -> ImportResult:
    """Import a statement and retain its workbook for later re-import."""
    backup_db(str(APP_DIR / 'finance.db'), str(APP_DIR / 'backups'))
    result = import_statement(conn, data, filename)
    if is_postgres(conn):
        st.info(
            'Хуулгыг Neon өгөгдлийн санд хадгаллаа. Эх Excel файл Render-ийн '
            'түр файлын системд архивлагдахгүй тул дахин оруулахдаа эх файлаа '
            'дахин сонгоно уу.'
        )
        return result
    file_hash = hashlib.sha256(data).hexdigest()
    archive_path = STATEMENT_ARCHIVE / f'{file_hash}.xlsx'
    try:
        STATEMENT_ARCHIVE.mkdir(parents=True, exist_ok=True)
        archive_path.write_bytes(data)
    except OSError:
        st.warning(
            'Хуулгыг өгөгдлийн санд оруулсан боловч дахин ашиглах файлыг '
            'хадгалж чадсангүй. Дискний зай, хавтас руу бичих эрхийг шалгана уу.'
        )
    return result


def render(conn: sqlite3.Connection) -> None:
    """Upload an Excel statement and show import results."""
    st.title('Банкны хуулга оруулах')
    file = st.file_uploader(
        'ХААН Банкны хуулга (.xlsx, 20 МБ хүртэл)',
        type=['xlsx'],
        help='20 МБ-аас ихгүй хэмжээтэй .xlsx файл сонгоно уу.',
    )
    if st.button('Оруулах', disabled=file is None, key='upload_statement'):
        if not file.name.lower().endswith('.xlsx'):
            st.error('.xlsx өргөтгөлтэй банкны хуулга сонгоно уу.')
        elif file.size > MAX_FILE_SIZE:
            st.error('Файлын хэмжээ 20 МБ-ын хязгаараас хэтэрсэн байна.')
        else:
            try:
                data = file.getvalue()
                if len(data) > MAX_FILE_SIZE:
                    st.error('Файлын хэмжээ 20 МБ-ын хязгаараас хэтэрсэн байна.')
                else:
                    st.session_state['import_result'] = _import_and_archive(
                        conn,
                        data,
                        file.name,
                    )
            except ValueError as exc:
                st.error(translate_import_message(str(exc)))
    result = st.session_state.get('import_result')
    if result:
        metrics = [
            ('Шинэ', result.rows_new),
            ('Давхардсан', result.rows_duplicate),
            ('Автоматаар ангилсан', result.auto_classified),
            ('Ангилах шаардлагатай', result.needs_review),
        ]
        for col, (label, value) in zip(st.columns(4), metrics):
            col.metric(label, value)
        for warning in result.warnings:
            st.warning(translate_import_message(warning))
        if st.button('Ангилах хэсэг рүү очих'):
            st.session_state['next_page'] = 'Review'
            st.rerun()
    st.subheader('Оруулсан хуулгын түүх')
    history = read_sql_query(
        '''SELECT filename,
                  CASE
                    WHEN period_start IS NULL THEN COALESCE(period_end, '')
                    WHEN period_end IS NULL THEN period_start
                    ELSE period_start || ' - ' || period_end
                  END AS period,
                  rows_new AS new, rows_duplicate AS dup, imported_at
           FROM imports ORDER BY id DESC''',
        conn,
    )
    st.dataframe(
        history,
        hide_index=True,
        column_config={
            'filename': 'Файлын нэр',
            'period': 'Хуулгын хугацаа',
            'new': 'Шинэ',
            'dup': 'Давхардсан',
            'imported_at': 'Оруулсан огноо',
        },
    )

    history_rows = conn.execute(
        '''SELECT id, filename, file_hash, period_start, period_end, imported_at
           FROM imports
           ORDER BY id DESC'''
    ).fetchall()
    if not history_rows:
        return

    history_options: dict[str, sqlite3.Row] = {}
    archive_available: dict[str, bool] = {}
    for row in history_rows:
        file_hash = row['file_hash']
        safe_hash = (
            isinstance(file_hash, str)
            and len(file_hash) == 64
            and all(character in '0123456789abcdef' for character in file_hash)
        )
        exists = safe_hash and (STATEMENT_ARCHIVE / f'{file_hash}.xlsx').is_file()
        period = ' – '.join(
            value
            for value in (row['period_start'], row['period_end'])
            if value
        ) or 'хугацаа тодорхойгүй'
        status = 'архивт хадгалсан' if exists else 'файлыг дахин сонгох шаардлагатай'
        label = (
            f"#{row['id']} · {row['imported_at']} · "
            f"{row['filename']} · {period} · {status}"
        )
        history_options[label] = row
        archive_available[label] = bool(exists)

    st.subheader('Өмнө оруулсан хуулгыг дахин оруулах')
    selected_label = st.selectbox(
        'Дахин оруулах хуулгаа сонгоно уу',
        list(history_options),
        key='reimport_statement_choice',
    )
    if not archive_available[selected_label]:
        st.info(
            'Энэ хуулгыг өмнө нь файл хэлбэрээр хадгалдаггүй байсан. '
            'Дахин оруулахын тулд дээрээс эх файлыг компьютероосоо сонгоно уу.'
        )
    if st.button(
        'Сонгосон хуулгыг дахин оруулах',
        disabled=not archive_available[selected_label],
        key='reimport_statement',
    ):
        selected_import = history_options[selected_label]
        selected_hash = selected_import['file_hash']
        archive_path = STATEMENT_ARCHIVE / f'{selected_hash}.xlsx'
        try:
            data = archive_path.read_bytes()
            if hashlib.sha256(data).hexdigest() != selected_hash:
                st.error(
                    'Хадгалсан хуулгын бүрэн бүтэн байдал алдагдсан байна. '
                    'Эх файлыг компьютероосоо дахин сонгоно уу.'
                )
            elif len(data) > MAX_FILE_SIZE:
                st.error('Хадгалсан файлын хэмжээ 20 МБ-ын хязгаараас хэтэрсэн байна.')
            else:
                st.session_state['import_result'] = _import_and_archive(
                    conn,
                    data,
                    selected_import['filename'],
                )
                st.success(
                    'Хуулгыг дахин уншлаа. Өмнө бүртгэсэн гүйлгээнүүдийг '
                    'давхардуулан нэмэхгүй.'
                )
                st.rerun()
        except ValueError as exc:
            st.error(translate_import_message(str(exc)))
        except OSError:
            st.error(
                'Хадгалсан хуулгыг уншиж чадсангүй. '
                'Файл устсан эсвэл унших эрхгүй байж болзошгүй.'
            )
