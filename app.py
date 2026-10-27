"""Streamlit-интерфейс для сервиса поиска поставщиков.

Запуск: streamlit run app.py
Логика (LLM-извлечение фильтров, SQL-поиск) целиком берётся из main.py.
"""

import sqlite3

from dotenv import load_dotenv

load_dotenv()

import streamlit as st
from pydantic import ValidationError

from main import (
    LLMError,
    Supplier,
    extract_filters,
    init_db,
    search_suppliers,
)

st.set_page_config(page_title="Поиск поставщиков", page_icon="🔎", layout="centered")
st.title("Поиск поставщиков")
st.write("Введите запрос на естественном языке, и LLM подберёт подходящих поставщиков.")

# Проверяем базу один раз на запуск страницы.
try:
    init_db()
except (FileNotFoundError, RuntimeError, sqlite3.Error) as exc:
    st.error(f"Ошибка инициализации: {exc}")
    st.stop()


def render_supplier(supplier: Supplier) -> None:
    """Рисует карточку одного поставщика."""
    with st.container(border=True):
        st.subheader(supplier.name)
        st.caption(
            f"{supplier.category} · "
            + ", ".join(filter(None, [supplier.city, supplier.region]))
        )
        col_left, col_right = st.columns(2)
        col_left.markdown(f"**Мин. партия:** {supplier.min_order:g} кг")
        col_left.markdown(f"**Цены:** {supplier.price_info or '—'}")
        col_right.markdown(f"**Доставка:** {'✅' if supplier.has_delivery else '❌'}")
        col_right.markdown(
            f"**Сертификаты:** {'✅' if supplier.has_certificates else '❌'}"
        )
        st.markdown(f"**Контакты:** {supplier.contacts or '—'}")
        if supplier.website:
            st.markdown(f"**Сайт:** {supplier.website}")


with st.form("search_form"):
    user_query = st.text_input(
        "Ваш запрос:",
        placeholder="Например: Найди в Москве упаковку с доставкой",
    )
    submitted = st.form_submit_button("Найти")

if submitted:
    if not user_query.strip():
        st.warning("Введите запрос")
        st.stop()

    try:
        with st.spinner("Анализирую запрос..."):
            filters = extract_filters(user_query)
    except LLMError as exc:
        st.error(f"Не удалось обработать запрос через LLM: {exc}")
        st.stop()

    if filters.is_empty():
        st.info("Фильтры не распознаны — ищу среди всех поставщиков.")
    else:
        with st.expander("Извлечённые фильтры", expanded=True):
            st.json(filters.model_dump(exclude_none=True))

    try:
        with st.spinner("Ищу поставщиков..."):
            suppliers = search_suppliers(filters)
    except (sqlite3.Error, ValidationError) as exc:
        st.error(f"Ошибка при работе с базой данных: {exc}")
        st.stop()

    if not suppliers:
        st.warning(
            "К сожалению, по вашему запросу ничего не найдено. "
            "Попробуйте смягчить условия."
        )
    else:
        st.success(f"Найдено поставщиков: {len(suppliers)}")
        for supplier in suppliers:
            render_supplier(supplier)