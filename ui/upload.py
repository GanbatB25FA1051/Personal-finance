"""Statement upload and import history."""
import hashlib
import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st

from core.db import backup_db, read_sql_query
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
    _archive_statement(data, hashlib.sha256(data).hexdigest())
    return result


def render(conn: sqlite3.Connection) -> None:
    """Upload an Excel statement and show import results."""
    st.title('Банкны хуулга оруулах')
    st.caption(
        'Шинэ хуулга өмнөх гүйлгээнүүдтэй нэгтгэгдэнэ. Давхардсан мөрийг '
        'дахин нэмэхгүй. Өмнөх хуулгаа дахин сонгох боломжтой бөгөөд хуулга '
        'бүрийн тайланг Тайлан хэсгээс тусад нь харна.'
    )
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
        '''SELECT i.id, i.filename, i.file_hash, i.period_start, i.period_end,
                  i.imported_at,
                  CASE WHEN f.import_id IS NULL THEN 0 ELSE 1 END AS stored_in_db
           FROM imports AS i
           LEFT JOIN import_files AS f ON f.import_id=i.id
           ORDER BY i.id DESC'''
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
        archive_exists = (
            safe_hash and (STATEMENT_ARCHIVE / f'{file_hash}.xlsx').is_file()
        )
        exists = bool(row['stored_in_db']) or archive_exists
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
    selected_import = history_options[selected_label]
    selected_hash = selected_import['file_hash']
    selected_id = selected_import['id']
    stored = conn.execute(
        'SELECT file_data, file_hash FROM import_files WHERE import_id=?',
        (selected_id,),
    ).fetchone()
    archive_path = STATEMENT_ARCHIVE / f'{selected_hash}.xlsx'
    if not archive_available[selected_label]:
        st.info(
            'Энэ өмнөх хуулгын Excel файл Render дээр хадгалагдаагүй байсан. '
            'Эх файлыг нэг удаа сонгож энэ хуулгатай холбоно уу. Шинэ хуулга '
            'нэмэх бол дээрх "Оруулах" хэсгийг ашиглана уу.'
        )
        recovery_file = st.file_uploader(
            'Өмнө оруулсан хуулгын эх Excel файлыг сонгох',
            type=['xlsx'],
            key=f'recover_statement_{selected_id}',
        )
        if st.button(
            'Эх файлыг сонгосон хуулгатай холбох',
            disabled=recovery_file is None,
            key=f'attach_statement_{selected_id}',
        ):
            if recovery_file is not None:
                data = recovery_file.getvalue()
                if len(data) > MAX_FILE_SIZE:
                    st.error('Файлын хэмжээ 20 МБ-ын хязгаараас хэтэрсэн байна.')
                elif hashlib.sha256(data).hexdigest() != selected_hash:
                    st.error(
                        'Сонгосон файл энэ хуулгатай таарахгүй байна. '
                        'Яг өмнө оруулсан Excel файлаа сонгоно уу.'
                    )
                else:
                    conn.execute(
                        '''INSERT INTO import_files(import_id, file_data, file_hash)
                           VALUES (?, ?, ?)
                           ON CONFLICT(import_id) DO UPDATE SET
                               file_data=excluded.file_data,
                               file_hash=excluded.file_hash
                           RETURNING import_id''',
                        (selected_id, data, selected_hash),
                    )
                    conn.commit()
                    _archive_statement(data, selected_hash)
                    st.success(
                        'Эх файлыг хуулгатай холболоо. Одоо дахин оруулж болно.'
                    )
                    st.rerun()
    if st.button(
        'Сонгосон хуулгыг дахин оруулах',
        disabled=not archive_available[selected_label],
        key='reimport_statement',
    ):
        try:
            if stored is not None:
                data = stored['file_data']
                stored_hash = stored['file_hash']
            else:
                data = archive_path.read_bytes()
                stored_hash = hashlib.sha256(data).hexdigest()
            if stored_hash != selected_hash or hashlib.sha256(data).hexdigest() != selected_hash:
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


def _archive_statement(data: bytes, file_hash: str) -> None:
    """Keep a local copy when the filesystem allows it."""
    archive_path = STATEMENT_ARCHIVE / f'{file_hash}.xlsx'
    try:
        STATEMENT_ARCHIVE.mkdir(parents=True, exist_ok=True)
        archive_path.write_bytes(data)
    except OSError:
        st.warning(
            'Файл өгөгдлийн санд хадгалагдсан боловч локал архив үүсгэж '
            'чадсангүй.'
        )
